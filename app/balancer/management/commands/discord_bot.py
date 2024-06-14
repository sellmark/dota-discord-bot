WAITING_TIME_MINS = 5

import asyncio
import datetime
import itertools
import re
from collections import defaultdict, deque
from datetime import timedelta
from datetime import datetime
from statistics import mean

import discord
from discord import Button, ButtonStyle, user
import pytz
import timeago
from discord.ext import tasks
from discord.channel import TextChannel
from django.core.management.base import BaseCommand
import os

from django.core.urlresolvers import reverse
from django.db.models import Q, Count, Case, When, F
from django.utils import timezone

from app.balancer.managers import BalanceResultManager, BalanceAnswerManager
from app.balancer.models import BalanceAnswer
from app.ladder.managers import MatchManager, QueueChannelManager
from app.ladder.models import Player, LadderSettings, LadderQueue, QueuePlayer, QueueChannel, MatchPlayer, \
    RolesPreference, DiscordChannels, DiscordPoll, ScoreChange, PlayerReport

from app.balancer.management.commands.discord.report_tip_commands import ReportTipCommands

from app.balancer.command_translation.translations import t, TRANSLATIONS, LANG

def is_player_registered(msg, dota_id, name):
    # check if we can register this player
    if Player.objects.filter(Q(discord_id=msg.author.id) | Q(dota_id=dota_id)).exists():
        return True
    if Player.objects.filter(name__iexact=name).exists():
        return True

def escape_underscore(player_name):
    return player_name.replace("_", "-")

escaping_pattern = (
    r'^[a-zA-Z0-9ąĄćĆęĘłŁńŃóÓśŚźŹżŻ\-\|\\/\s\!\@\#\$\%\^\&\*\(\)\_\+]*'  # Allows alphanumeric, hyphen, underscore, pipe, backslash, forward slash, spaces, and special characters from SHIFT+1 to SHIFT+0 on a US keyboard, plus pluses
    r'('
        r'[\U0001F600-\U0001F64F]'  # Emoticons
        r'|[\U0001F300-\U0001F5FF]'  # Misc Symbols and Pictographs
        r'|[\U0001F680-\U0001F6FF]'  # Transport and Map Symbols
        r'|[\U0001F700-\U0001F77F]'  # Alchemical Symbols
        r'|[\U0001F780-\U0001F7FF]'  # Geometric Shapes Extended
        r'|[\U0001F800-\U0001F8FF]'  # Supplemental Arrows-C
        r'|[\U0001F900-\U0001F9FF]'  # Supplemental Symbols and Pictographs
        r'|[\U0001FA00-\U0001FA6F]'  # Chess Symbols
        r'|[\U0001FA70-\U0001FAFF]'  # Symbols and Pictographs Extended-A
        r'|[\U00002702-\U000027B0]'  # Dingbats
        r'|[\U000024C2-\U0001F251]'  # Enclosed characters
    r')*'
    r'[a-zA-Z0-99ąĄćĆęĘłŁńŃóÓśŚźŹżŻ\-\|\\/\s\!\@\#\$\%\^\&\*\(\)\_\+]*$'  # Ending with any amount of alphanumeric and allowed symbols
)

class Command(BaseCommand):
    REGISTER_MSG_TEXT = t("register_msg")

    def __init__(self):
        super().__init__()
        self.bot = None
        self.polls_channel = None
        self.queues_channel = None
        self.chat_channel = None
        self.voice_channel = None
        self.status_message = None  # status msg in queues channel
        self.status_responses = deque(maxlen=3)
        self.last_seen = defaultdict(timezone.now)  # to detect afk players
        self.kick_votes = defaultdict(lambda: defaultdict(set))
        self.queued_players = set()
        self.last_queues_update = timezone.now()
        self.report_tip_commands = ReportTipCommands()

        # cached discord models
        self.queue_messages = {}

        # self.poll_commands = PollService(self.bot)
        #
        # self.poll_reaction_funcs = {
        #     'DraftMode': self.poll_commands.on_draft_mode_reaction,
        #     'EliteMMR': self.poll_commands.on_elite_mmr_reaction,
        #     'Faceit': self.poll_commands.on_faceit_reaction,
        # }

    def handle(self, *args, **options):
        bot_token = os.environ.get('DISCORD_BOT_TOKEN', '')

        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True

        self.bot = discord.Client(intents=intents)

        @self.bot.event
        async def on_ready():
            print(f'Logged in: {self.bot.user} {self.bot.user.id}')
            for guild in self.bot.guilds:
                print(f"- {guild.name} (ID: {guild.id})")

            # for emoji in self.bot.emojis:
            #     print(f"{emoji.name}: {emoji.id}")
            queues_channel = DiscordChannels.get_solo().queues
            chat_channel = DiscordChannels.get_solo().chat
            queue_status_voice_channel = DiscordChannels.get_solo().queue_counter
            self.queues_channel = self.bot.get_channel(queues_channel)
            self.chat_channel = self.bot.get_channel(chat_channel)
            self.voice_channel = self.bot.get_channel(queue_status_voice_channel)

            # It's available in PollCommands but turned off.
            # await self.poll_commands.setup_poll_messages()

            # We don't have enough privileges to do it this way
            # await self.purge_queue_channels()

            await self.setup_queue_messages()

            # MAREK: Disabled checking for AFK players
            queue_afk_check.start()

            update_queues_shown.start()
            update_voice_channel.start()

            # It needs too high privileges to channel.purge() or 2FA for bot
            # clear_queues_channel.start()

            activate_queue_channels.start()
            deactivate_queue_channels.start()

        async def on_register_form_answer(message):
            # Check if the message is a response to the exact form, User has invoked
            if message.author != self.bot.user and message.reference:
                original_message = await message.channel.fetch_message(message.reference.message_id)
                if original_message.author == self.bot.user:
                    fields = message.content.split(',')
                    fields.append(message.author.name)
                    print(f"Fields: {fields}")
                    mmr = int(fields[0])
                    steam_id = str(int(fields[1]))
                    discord_main = fields[2]
                    await self.register_new_player(message, discord_main, mmr, steam_id)


        @self.bot.event
        async def on_message(msg):
            await on_register_form_answer(msg)

            if msg.guild is None:
                await self.bot_cmd(msg)
                return
            self.last_seen[msg.author.id] = timezone.now()

            if not QueueChannel.objects.filter(discord_id=msg.channel.id).exists() \
               and not (msg.channel.id == DiscordChannels.get_solo().chat):
                return
            if msg.author.bot:
                return

            msg.content = " ".join(msg.content.split())
            if msg.content.startswith('!'):
                # Process with a bot command
                await self.bot_cmd(msg)

        @self.bot.event
        async def on_raw_reaction_add(payload):
            user = self.bot.get_user(payload.user_id)

            self.last_seen[user.id] = timezone.now()
            if user.bot:
                return

        @self.bot.event
        async def on_button_click(interaction: discord.Interaction, button):
            self.last_seen[interaction.user.id] = timezone.now()
            button_parts = button.custom_id.split('-')
            if len(button_parts) != 2:
                print("Invalid button custom_id format.")
                return

            type, value = button_parts
            print(button_parts)

            player = Player.objects.filter(discord_id=interaction.user.id).first()

            if not player:
                if type != 'register_form':
                    await interaction.defer()
                    return

                text = t("register_msg").format({self.unregistered_mention(interaction.author)})

                await interaction.author.send(text)

                await interaction.defer()

                return

            if type == 'green':
                q_channel = QueueChannel.objects.filter(discord_msg=value).first()

                _, _, response= await self.player_join_queue(player, q_channel)
                embed = discord.Embed(title=t("queue_join"),
                                      description=response,
                                      color=discord.Color.green())
                # We can also send self-hiding responses to the message via:
                # await interaction.respond(embed=embed, allowed_mentions=None, delete_after=5)
                await interaction.edit(embed=embed)

            elif type == 'red':
                await self.player_leave_queue(player, interaction.message)
                await interaction.defer()

            elif type == 'vouch':
                vouched_player = Command.get_player_by_name(value)

                if not player.bot_access:
                    await interaction.defer()
                    return

                if not vouched_player:
                    embed = discord.Embed(title= t("vouch_error"),
                                          color=discord.Color.red())
                    await interaction.message.edit(embed=embed)
                    return

                await self.player_vouched(vouched_player)
                embed = discord.Embed(title=t("player_vouch"),
                                      description=t("approved_by").format(value, player.name),
                                      color=discord.Color.blue())
                await interaction.edit(embed=embed)
                await self.purge_buttons_from_msg(interaction.message)

            if type in ["green", "red"]:
                await self.queues_show()

        @tasks.loop(seconds=302)
        async def update_voice_channel():
            await self.voice_channel.edit(name=t("signed_players").format(f"{len(self.queued_players)}/10"))

        @tasks.loop(minutes=5)
        async def queue_afk_check():
            print("Queue clear")
            # TODO: it would be good to do here
            #  .select_related(`player`, `queue`, `queue__channel`)
            #  but this messes up with itertools.groupby.
            #  Need to measure speed here and investigate.
            players = QueuePlayer.objects\
                .filter(queue__active=True)\
                .annotate(Count('queue__players'))\
                .filter(queue__players__count__lt=10)

            # group players by channel
            players = itertools.groupby(players, lambda x: x.queue.channel)

            for channel, qp_list in players:
                channel_players = [qp.player for qp in qp_list]

                channel = self.bot.get_channel(channel.discord_id)
                await self.channel_check_afk(channel, channel_players)

        @tasks.loop(seconds=15)
        async def update_queues_shown():
            queued_players = [qp for qp in QueuePlayer.objects.filter(queue__active=True)]
            queued_players = set(qp.player.discord_id for qp in queued_players)

            outdated = timezone.now() - self.last_queues_update > timedelta(minutes=3)
            if queued_players != self.queued_players or outdated:
                await self.queues_show()


        @tasks.loop(minutes=1)
        async def activate_queue_channels():
            dt = timezone.localtime(timezone.now(), pytz.timezone('CET'))
            if dt.hour == 0 and dt.minute == 0:
                print('Activating queue channels.')
                QueueChannelManager.activate_qchannels()
                await self.setup_queue_messages()

        @tasks.loop(minutes=1)
        async def deactivate_queue_channels():
            dt = timezone.localtime(timezone.now(), pytz.timezone('CET'))
            if dt.hour == 8 and dt.minute == 0:
                print('Deactivating queue channels')
                QueueChannelManager.deactivate_qchannels()
                await self.setup_queue_messages()

        @tasks.loop(minutes=5)
        async def clear_queues_channel():
            channel = DiscordChannels.get_solo().queues
            channel = self.bot.get_channel(channel)

            db_messages = QueueChannel.objects.filter(active=True).values_list('discord_msg', flat=True)

            def should_remove(msg):
                msg_time = msg.edited_at or msg.created_at
                lifetime = timedelta(minutes=5)
                outdated = timezone.now() - timezone.make_aware(msg_time) > lifetime

                return (msg.id not in db_messages) and outdated

            await channel.purge(check=should_remove)

        self.bot.run(bot_token)

    async def bot_cmd(self, msg):
        command = msg.content.split(' ')[0].lower()

        commands = self.get_available_bot_commands()
        direct_message = ['!report']
        free_for_all = ['!register', '!help', '!reg', '!r', '!jak', '!info', '!list', '!q']
        staff_only = [
            '!vouch', '!add', '!kick', '!mmr', '!ban', '!unban',
            '!set-name', '!set-mmr', '!set-dota-id', '!record-match', '!record-queue'
            '!close', '!adminhelp',
        ]
        # TODO: do something with this, getting too big. Replace with disabled_in_chat list?
        chat_channel = [
            '!register', '!vouch', '!wh', '!who', '!whois', '!profile', '!stats', '!top',
            '!streak', '!bottom', '!bot', '!afk-ping', '!afkping', '!role', '!roles', '!recent',
            '!ban', '!unban', '!votekick', '!vk', '!set-name', '!set-mmr', '!adminhelp',
            '!adjust', '!set-dota-id', '!record-match', '!record-queue', '!help', '!close',
            '!reg', '!r', '!rename', '!jak', '!info', '!list', '!q', 'rename', '!my-dota-id',
            '!report', '!tip', '!reports', '!tips',
        ]

        if msg.guild is None:
            if command not in direct_message:
                return
            print("command in direct mess")
            msg.channel = msg.author
            await commands[command](msg)
            return

        # if this is a chat channel, check if command is allowed
        if msg.channel.id == DiscordChannels.get_solo().chat:
            if command not in chat_channel:
                return

        # if command is free for all, no other checks required
        if command in free_for_all:
            await commands[command](msg)
            return

        # get player from DB using discord id
        try:
            player = Player.objects.get(discord_id=msg.author.id)
        except Player.DoesNotExist:
            mention = self.unregistered_mention(msg.author)
            print(mention)
            await msg.channel.send(t("unregistered_command").format(mention))
            return

        if player.banned:
            await msg.channel.send(t("banned").format(msg.author.name))
            return

        # check permissions when needed
        if not player.bot_access:
            # only staff can use this commands
            if command in staff_only:
                await msg.channel.send(t("staff_only").format(msg.author.name))
                return

        # user can use this command
        await commands[command](msg, **{'player': player})

    async def register_command(self, msg, **kwargs):
        command = msg.content
        print()
        print('!register command')
        print(command)

        try:
            params = command.split(None, 1)[1]  # get params string
            params = params.rsplit(None, 2)  # split params string into a list

            name = params[0]
            mmr = int(params[1])
            dota_id = str(int(params[2]))  # check that id is a number
        except (IndexError, ValueError):
            await msg.channel.send(t("register_format"))
            return

        if not 0 <= mmr < 12000:
            sent_message = await msg.channel.send(t("very_funny"))

            return

        await self.register_new_player(msg, name, mmr, dota_id)

    async def register_new_player(self, msg, name, mmr, dota_id):
        if is_player_registered(msg, dota_id, name):
            await msg.channel.send(t("already_registered"))
            return

        # all is good, can register
        player = Player.objects.create(
            name=name,
            dota_mmr=mmr,
            dota_id=dota_id,
            discord_id=msg.author.id,
        )

        Player.objects.update_ranks()

        await self.player_vouched(player)

        queue_channel = DiscordChannels.get_solo().queues
        chat_channel = DiscordChannels.get_solo().chat
        channel = self.bot.get_channel(chat_channel)
        if msg.guild is None:
            await msg.channel.send(t("welcome").format(name, queue_channel))

        await channel.send(t("welcome").format(name, queue_channel))


    async def vouch_command(self, msg, **kwargs):
        command = msg.content
        print()
        print('Vouch command:')
        print(command)

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        await self.player_vouched(player)

        await msg.channel.send(t("vouched").format(self.player_mention(player)))

    async def whois_command(self, msg, **kwargs):
        command = msg.content
        print()
        print('Whois command:')
        print(command)

        player = name = None
        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            #  if name is not provided, show current player
            player = kwargs['player']

        player = player or Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        dotabuff = f'https://www.dotabuff.com/players/{player.dota_id}'

        host = os.environ.get('BASE_URL', 'localhost:8000')
        url = reverse('ladder:player-overview', args=(player.slug,))
        player_url = f'{host}{url}'

        season = LadderSettings.get_solo().current_season
        player.matches = player.matchplayer_set \
            .filter(match__season=season) \
            .select_related('match')
        wins = sum(1 if m.match.winner == m.team else 0 for m in player.matches)
        losses = len(player.matches) - wins
        # Query for reports and tips count
        reports_count = PlayerReport.objects.filter(to_player=player, value__lt=0).count()
        tips_count = PlayerReport.objects.filter(to_player=player, value__gt=0).count()

        mps = player.matchplayer_set.filter(match__season=LadderSettings.get_solo().current_season)
        results = ['win' if x.team == x.match.winner else 'loss' for x in mps]
        streaks = [list(g) for k, g in itertools.groupby(results)]
        streak = {}
        max_streak = {}
        losing_streak = 0
        winning_streak = 0
        escaped_name = player.name.replace('_', '-')

        if len(streaks):
            streak = streaks[0]
            max_streak = max(streaks, key=len)
            for s in streaks:
                if len(s) > winning_streak and s[0] == "win":
                    winning_streak = len(s)
                if len(s) > losing_streak and s[0] == "loss":
                    losing_streak = len(s)

        await msg.channel.send(t("whois_stats").format(
            escaped_name,
            player.rank_score,
            player.ladder_mmr,
            player.dota_mmr,
            len(player.matches),
            round(wins/(wins+losses)*100) if wins+losses != 0  else 0,
            f'{"+" if streak and streak[0] == "win" else "-"}{len(streak)}',
            winning_streak,
            losing_streak,
            tips_count,
            reports_count,
            player.roles.carry,
            player.roles.mid,
            player.roles.offlane,
            player.roles.pos4,
            player.roles.pos5,
            player_url,
            dotabuff,
            )
        )

    async def handle_show_tips_command(self, msg, **kwargs):
        # Extract the player name from the message
        player = kwargs['player']

        parts = msg.content.split(maxsplit=1)
        if len(parts) == 2:
            player_name = parts[1]
        else:
            player_name = player.name


        try:
            player = Command.get_player_by_name(player_name)
        except Player.DoesNotExist:
            await msg.channel.send(f"Player '{player_name}' not found.")
            return

        # Fetch all tips for the player
        tips = PlayerReport.objects.filter(to_player=player, value__gt=0).order_by('-id')[:5]

        if tips.exists():
            tips_list = '\n'.join(
                [f"│ 👍 <{escape_underscore(tip.from_player.name)}> [{tip.comment}] (Mecz: {tip.match.dota_id if tip.match else 'N/A'})" for tip in
                 tips])
            await msg.channel.send(t("show_tips").format(escape_underscore(player.name), tips_list));
        else:
            await msg.channel.send(t("no_tips").format(escape_underscore(player.name)));

    async def handle_show_reports_command(self, msg, **kwargs):
        # Extract the player name from the message
        player = kwargs['player']

        parts = msg.content.split(maxsplit=1)
        if len(parts) == 2:
            player_name = parts[1]
        else:
            player_name = player.name

        try:
            player = Command.get_player_by_name(player_name)
        except Player.DoesNotExist:
            await msg.channel.send(t("report_could_not_list_reports").format(player_name))
            return

        one_day_ago = timezone.now() - timedelta(days=1)

        # Fetch all tips for the player
        reports = PlayerReport.objects.filter(to_player=player, value__lt=0, report_date__lt=one_day_ago).order_by('-id')[:5]

        if reports.exists():
            tips_list = '\n'.join(
                [f"│ ☢️ <{escape_underscore(report.from_player.name)}> [{report.comment}] (Mecz: {report.match.dota_id if report.match else 'N/A'})" for report in
                 reports])
            await msg.channel.send(t("show_reports").format(escape_underscore(player.name), tips_list))
        else:
            await msg.channel.send(t("no_reports").format(escape_underscore(player.name)))

    async def ban_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'Ban command from {player}:\n {command}')

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        player.banned = Player.BAN_PLAYING
        player.save()

        await msg.channel.send(t("ban_message").format(player.name))

    async def unban_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'Unban command from {player}:\n {command}')

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        player.banned = None
        player.save()

        await msg.channel.send(t("unban_message").format(player.name))

    async def attach_join_buttons_to_queue_msg(self, msg, **kwargs):
        await self.attach_buttons_to_msg(msg, [
            [
                Button(label="Dołącz",
                       custom_id="green-" + str(msg.id),
                       style=ButtonStyle.green),
                Button(label="Opuść",
                       custom_id="red-" + str(msg.id),
                       style=ButtonStyle.red),
            ]
        ])

    async def attach_buttons_to_msg(self, msg, buttons, **kwargs):
        await msg.edit(components=buttons)
    
    async def purge_buttons_from_msg(self, msg):
        await msg.edit(components=[])

    async def attach_help_buttons_to_msg(self, msg):
        if is_player_registered(msg, 0, "blank"):
            await msg.channel.send(t("already_registered"))
            return

        await msg.author.send(components=[
        [Button(label="!register", custom_id="register_form-"+str(msg.channel.id), style=ButtonStyle.blurple)]
    ])

    ##TODO: finish translations for this method(was removed by mistake?)
    async def leave_queue_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'Leave command from {player}:\n {command}')

        await self.player_leave_queue(player, msg)
        await self.queues_show()

    async def show_queues_command(self, msg, **kwargs):
        queues = LadderQueue.objects.filter(active=True)
        if queues:
            await msg.channel.send(
                ''.join(Command.queue_str(q) for q in queues)
            )
        else:
            await msg.channel.send('No one is currently queueing.')

        await self.queues_show()

    async def add_to_queue_command(self, msg, **kwargs):
        command = msg.content
        print(f'Add_to_queue command from:\n {command}')

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        # check that player is not in a queue already
        if player.ladderqueue_set.filter(active=True):
            await msg.channel.send(t("already_in_queue").format(player))
            return

        channel = QueueChannel.objects.get(discord_id=msg.channel.id)
        queue = Command.add_player_to_queue(player, channel)

        await msg.channel.send(t("forced_queue").format(msg.author, self.player_mention(player)))

        # TODO: this is a separate function
        if queue.players.count() == 10:
            Command.balance_queue(queue)

            balance_str = ''
            if LadderSettings.get_solo().draft_mode == LadderSettings.AUTO_BALANCE:
                balance_str = f'Proposed balance: \n' + \
                              Command.balance_str(queue.balance)

            ##TODO - multiline arguments
            await msg.channel.send(t("queue_full").format(balance_str, ' '.join(self.player_mention(p) for p in queue.players.all()), WAITING_TIME_MINS))

        await self.queues_show()

    async def kick_from_queue_command(self, msg, **kwargs):
        command = msg.content
        print(f'Kick command:\n {command}')

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        deleted, _ = QueuePlayer.objects \
            .filter(player=player, queue__active=True) \
            .delete()

        if deleted > 0:
            player_discord = self.bot.get_user(int(player.discord_id))
            mention = player_discord.mention if player_discord else player.name
            await msg.channel.send(t("queue_kick").format(mention))
        else:
            await msg.channel.send(t("not_in_queue").format())

        await self.queues_show()

    async def votekick_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'Vote kick command from {player}:\n {command}')

        try:
            name = command.split(None, 1)[1]
        except (IndexError, ValueError):
            return

        queue = player.ladderqueue_set.filter(active=True).first()
        if not queue or queue.players.count() < 10:
            await msg.channel.send(f'`{player}`, you are not in a full queue.')
            return

        victim = Command.get_player_by_name(name)
        if not victim:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        if victim not in queue.players.all():
            await msg.channel.send(t("victim_not_in_queue").format(victim, player))
            return

        votes_needed = LadderSettings.get_solo().votekick_treshold
        # TODO: im memory becomes an issue, use queue and player ids instead of real objects
        votes = self.kick_votes[queue][victim]
        votes.add(player)

        voters_str = " 💠 ".join(player.name for player in votes)
        await msg.channel.send(t("vote_kick").format(len(votes), votes_needed, victim, voters_str))

        if len(votes) >= votes_needed:
            QueuePlayer.objects \
                .filter(player=victim, queue__active=True) \
                .delete()

            del self.kick_votes[queue][victim]

            victim_discord = self.bot.get_user(int(victim.discord_id))
            mention = victim_discord.mention if victim_discord else victim.name
            await msg.channel.send(t("vote_kicked").format(mention))

            await self.queues_show()

    async def mmr_command(self, msg, **kwargs):
        command = msg.content
        print(f'\n!mmr command:\n{command}')

        try:
            min_mmr = int(command.split(' ')[1])
            min_mmr = max(0, min(9000, min_mmr))
        except (IndexError, ValueError):
            return

        channel = QueueChannel.objects.get(discord_id=msg.channel.id)

        if LadderQueue.objects.filter(channel=channel, active=True).exists():
            await msg.channel.send(t("cannot_change_mmr"))
            return

        channel.min_mmr = min_mmr
        channel.save()

        await msg.channel.send(t("min_mmr").format(min_mmr))

    async def top_command(self, msg, **kwargs):
        def get_top_players(limit, bottom=False):
            season = LadderSettings.get_solo().current_season
            qs = Player.objects \
                .order_by('-score', '-ladder_mmr') \
                .filter(matchplayer__match__season=season).distinct()\
                .annotate(
                    match_count=Count('matchplayer'),
                    wins=Count(Case(
                        When(
                            matchplayer__team=F('matchplayer__match__winner'), then=1)
                    )
                    ),
                    losses=F('match_count') - F('wins'),
                )
            players = qs[:limit]
            if bottom:
                players = reversed(players.reverse())
            return players

        def player_str(p):
            # pretty format is tricky
            # TODO: let's move to discord embeds asap
            # Escape underscores in p.name
            escaped_name = p.name.replace('_', '\_')
            name_offset = 25 - len(escaped_name)

            result = f'<{p.name}>{"." * name_offset}[{p.score}][{p.ladder_mmr}] {p.wins}W-{p.losses}P'
            return result

        command = msg.content
        bottom = kwargs.get('bottom', False)  # for '!bottom' command
        print(f'\n!top command:\n{command}')

        if LadderSettings.get_solo().casual_mode:
            joke_top = TRANSLATIONS[EN]["joke_top"]
            joke_bot = TRANSLATIONS[EN]["joke_bot"]
            await msg.channel.send(TRANSLATIONS[EN]["joke"].format(joke_bot if bottom else joke_top))
            return

        try:
            limit = int(command.split(' ')[1])
        except IndexError:
            limit = 10  # default value
        except ValueError:
            return

        host = os.environ.get('BASE_URL', 'localhost:8000')
        url = f'{host}{reverse("ladder:player-list-score")}'

        if limit < 1:
            await msg.channel.send(t("very_funny"))
            return

        if limit > 15:
            await msg.channel.send(t("just_open").format(url))
            return

        # all is ok, can show top players
        players = get_top_players(limit, bottom)
        top_str = '\n'.join(
            f'{p.rank_score:2}. {player_str(p)}' for p in players
        )
        await msg.channel.send(t("full_leaderboard").format(top_str, url))

    async def bottom_command(self, msg, **kwargs):
        print(f'\n!bottom command:\n{msg.content}')

        kwargs.update({'bottom': True})
        await self.top_command(msg, **kwargs)

    async def role_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'\n!role command from {player}:\n{command}')

        roles = player.roles
        args = command.split(' ')[1:]

        if len(args) == 5:
            # full roles format; check that we have 5 numbers from 1 to 5
            try:
                args = [int(x) for x in args]
                if any(not 0 < x < 6 for x in args):
                    raise ValueError
            except ValueError:
                await msg.channel.send(t("very_funny"))
                return

            # args are fine
            roles.carry = args[0]
            roles.mid = args[1]
            roles.offlane = args[2]
            roles.pos4 = args[3]
            roles.pos5 = args[4]
        elif len(args) == 2:
            # !role mid 4  format
            try:
                role = args[0]
                value = int(args[1])
                if not 0 < value < 6:
                    raise ValueError

                if role in ['carry', 'pos1']:
                    roles.carry = value
                elif role in ['mid', 'midlane', 'pos2']:
                    roles.mid = value
                elif role in ['off', 'offlane', 'pos3']:
                    roles.offlane = value
                elif role in ['pos4']:
                    roles.pos4 = value
                elif role in ['pos5']:
                    roles.pos5 = value
                elif role in ['core']:
                    roles.carry = roles.mid = roles.offlane = value
                elif role in ['sup', 'supp', 'support']:
                    roles.pos4 = roles.pos5 = value
                else:
                    raise ValueError  # wrong role name
            except ValueError:
                await msg.channel.send(t("very_funny"))
                return
        else:
            # !role command without args, show current role prefs
            await msg.channel.send(t("current_roles").format(player.name, roles.carry, roles.mid, roles.offlane, roles.pos4, roles.pos5))
            return

        roles.save()
        await msg.channel.send(t("current_roles").format(player.name, roles.carry, roles.mid, roles.offlane, roles.pos4, roles.pos5))

    async def recent_matches_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'\n!recent command from {player}:\n{command}')

        # possible formats:
        #   !recent
        #   !recent 10
        #   !recent jedi judas
        #   !recent jedi judas 10
        name = None
        num = 5
        try:
            params = command.split(None, 1)[1]  # get params string
            try:
                # check if matches num present
                num = int(params.split()[-1])
                name = ' '.join(params.split()[:-1])  # remove number of games, leaving only the name
            except ValueError:
                # only name is present
                name = params
        except IndexError:
            pass  # no params given, use defaults

        if name:
            player = Command.get_player_by_name(name)
            if not player:
                await msg.channel.send(t("unregistered_user").format(name))
                return

        host = os.environ.get('BASE_URL', 'localhost:8000')
        url = reverse('ladder:player-overview', args=(player.slug,))
        player_url = f'{host}{url}'

        if not 0 < num < 10:
            await msg.channel.send(t("just_open").format(player_url))
            return

        mps = player.matchplayer_set.all()[:num]
        for mp in mps:
            mp.result = "🟢 Wygrana " if mp.team == mp.match.winner else "🔴 Przegrana "

        def match_str(mp):
            dotabuff = f'https://www.dotabuff.com/matches/{mp.match.dota_id}'
            return f'│ ` {timeago.format(mp.match.date, timezone.now()):<15}{mp.result:<13}{dotabuff}'

        await msg.channel.send(t("recent_matches").format(player, '\n'.join(match_str(x) for x in mps), player_url))

    async def help_command(self, msg, **kwargs):
        commands_dict = self.get_help_commands()
        master_text = ''

        for group, texts in commands_dict.items():
            for key, text in texts.items():
                master_text += key + ": `" + text + "`\n"

        await msg.channel.send(t("help_command").format(master_text))

    async def admin_help_command(self, msg, **kwargs):
        commands_dict = self.get_admin_help_commands()
        master_text = ''

        for group, texts in commands_dict.items():
            for key, text in texts.items():
                master_text += "- " + key + ": `" + text + "`\n"

        await msg.channel.send(t("admin_help_command").format(master_text))

    async def registration_help_command(self, msg, **kwargs):
        print('!jak command')
        queue_channel = DiscordChannels.get_solo().queues
        # \nMożesz dołączyć do gry na kanale <#{queue_channel}>"""
        await msg.channel.send(t("registration_help").format(queue_channel))

    async def set_name_command(self, msg, **kwargs):
        command = msg.content
        admin = kwargs['player']

        print(f'\n!set-name command from {admin}:\n{command}')

        try:
            params = command.split(None, 1)[1]  # get params string
            mention = params.split()[0]
            new_name = ' '.join(params.split()[1:])  # rest of the string is a new name
        except (IndexError, ValueError):
            await msg.channel.send(t("wrong_set_name_usage"))
            return

        # check if name is a mention
        match = re.match(r'<@!?([0-9]+)>$', mention)
        if not match:
            await msg.channel.send(t("wrong_set_name_usage"))
            return

        player = Command.get_player_by_name(mention)
        if not player:
            await msg.channel.send(t("unregistered_user").format(mention))
            return

        player.name = new_name
        player.save()
        await msg.channel.send(t("name_change").format(mention, new_name))

    async def rename_myself_command(self, msg, **kwargs):
        min_length = 3
        max_length = 32
        command = msg.content
        print(f'\n!rename command from {msg.author.name}:\n{command}')

        player = Player.objects.filter(discord_id=msg.author.id).first()
        if not player:
            await msg.channel.send(t("unregistered_command").format(msg.author.mention))
            return

        try:
            new_name = command.split(' ', 1)[1].strip()  # Everything after "!rename"
        except IndexError:
            await msg.channel.send(t("wrong_rename_usage"))
            return



        if not re.fullmatch(escaping_pattern, new_name) or len(new_name) > max_length or len(new_name) < min_length:
            await msg.channel.send(t("invalid_nickname").format(self.player_mention(player)))
            return

        player.name = new_name
        player.save()
        await msg.channel.send(t("name_change").format(self.player_mention(player), new_name))

    async def change_self_dota_id(self, msg, **kwargs):
        command = msg.content
        print(f'\n!my-dota-id command from {msg.author.name}:\n{command}')

        player = Player.objects.filter(discord_id=str(msg.author.id)).first()
        if not player:
            await msg.channel.send(t("unregistered_user").format(self.unregistered_mention(msg.author)))
            return

        try:
            _, friend_id = command.split(' ', 1)
        except (ValueError, IndexError):
            await msg.channel.send(t("!my_dota_id_usage"))
            return

        if friend_id.isdigit():
            player.dota_id = friend_id
            player.save()  # Save the updated Player object to the database
            await msg.channel.send(t("player_updated_dota_id").format(
                self.player_mention(player),
                friend_id)
            )

            return
        else:
            await msg.channel.send('The Dota ID provided is not valid. Please ensure it is a numeric value.')

    async def set_mmr_command(self, msg, **kwargs):
        command = msg.content
        admin = kwargs['player']
        print(f'\n!set-mmr command from {admin}:\n{command}')

        try:
            params = command.split(None, 1)[1]  # get params string
            new_mmr = int(params.split()[-1])
            name = ' '.join(params.split()[:-1])  # remove mmr, leaving only the name
        except (IndexError, ValueError):
            await msg.channel.send(t("wrong_set_mmr_usage"))
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        ScoreChange.objects.create(
            player=player,
            mmr_change=(new_mmr - player.ladder_mmr),
            season=LadderSettings.get_solo().current_season,
            info=f'Admin action. MMR updated by {admin}'
        )
        await msg.channel.send(t("mmr_change").format(player, new_mmr))

    async def set_dota_id_command(self, msg, **kwargs):
        command = msg.content
        admin = kwargs['player']
        print(f'\n!set-dota-id command from {admin}:\n{command}')

        try:
            params = command.split(None, 1)[1]  # get params string
            dota_id = params.split()[-1]
            name = ' '.join(params.split()[:-1])  # remove dota id, leaving only the name
        except (IndexError, ValueError):
            await msg.channel.send(t("wrong_set_id_usage"))
            return

        player = Command.get_player_by_name(name)
        if not player:
            await msg.channel.send(t("unregistered_user").format(name))
            return

        player.dota_id = dota_id
        player.save()
        await msg.channel.send(t("id_change").format(player, dota_id))

    async def record_match_command(self, msg, **kwargs):
        command = msg.content
        admin = kwargs['player']
        print(f'\n!record-match command from {admin}:\n{command}')

        try:
            params = command.split(None, 1)[1]  # get params string
            winner = params.split()[0].lower()
            players = ' '.join(params.split()[1:])  # rest of the string are 10 player mentions
        except (IndexError, ValueError):
            await msg.channel.send(t("wrong_record_usage"))
            return

        if winner not in ['radiant', 'dire']:
            await msg.channel.send(t("wrong_winner"))
            return

        players = re.findall(r'<@!?([0-9]+)>', players)
        players = list(dict.fromkeys(players))  # remove duplicates while preserving order

        # check if we have 10 mentions of players
        if len(players) != 10:
            await msg.channel.send(t("wrong_record_usage"))
            return

        radiant = Player.objects.filter(discord_id__in=players[:5])
        dire = Player.objects.filter(discord_id__in=players[5:])

        # check if all mentioned players are registered as players
        if len(radiant) != 5 or len(dire) != 5:
            await msg.channel.send(t("unregistered_mentioned"))
            return

        _radiant = [(p.name, p.ladder_mmr) for p in radiant]
        _dire = [(p.name, p.ladder_mmr) for p in dire]
        winner = 0 if winner == 'radiant' else 1

        balance = BalanceAnswerManager.balance_custom([_radiant, _dire])
        MatchManager.record_balance(balance, winner)

        await msg.channel.send(t("match_recorded").format(", ".join([p.name for p in radiant]), ", ".join([p.name for p in dire]), "Radiant" if winner == 0 else "Dire"))

    async def record_match_from_queue(self, msg, **kwargs):
        command = msg.content
        admin = kwargs['player']
        print(f'\n!record-queue command from {admin}:\n{command}')

        try:
            params = command.split()  # get params string
            winner = params[1].lower()
            queue_id = int(params[2])  # extract queue_id
        except (IndexError, ValueError):
            await msg.channel.send(
                'Wrong command usage. '
                'Correct example: `!record-queue dire 1234`')
            return

        if winner not in ['radiant', 'dire']:
            await msg.channel.send(
                f'Scientists are baffled. Dota has 2 teams: `radiant` and `dire`. '
                f'You invented a third one: `{winner}`. Congratulations!')
            return

        try:
            # Fetch the LadderQueue instance
            queue = LadderQueue.objects.get(id=queue_id)
            # Extract the BalanceAnswer from the queue instance

            radiant, dire, radiant_mmr, dire_mmr = Command.get_teams_from_queue(queue)

            _radiant = [(p.name, p.ladder_mmr) for p in radiant]
            _dire = [(p.name, p.ladder_mmr) for p in dire]
            winner = 0 if winner == 'radiant' else 1

            # print([_radiant, _dire])
            balance = BalanceAnswerManager.balance_custom([_radiant, _dire])
            MatchManager.record_balance(balance, winner)

            await msg.channel.send(
                f'```\n' +
                f'Match recorded!\n\n' +
                f'Radiant: {", ".join([p.name for p in radiant])}\n' +
                f'Dire: {", ".join([p.name for p in dire])}\n' +
                f'\n{"Radiant" if winner == 0 else "Dire"} won.\n'
                f'\n```'
            )
        except LadderQueue.DoesNotExist:
            await msg.channel.send(f'Error: Queue with ID {queue_id} does not exist.')
            return
        except AttributeError:
            print('Error: Failed to extract players from balance answer.')
            return

    async def close_queue_command(self, msg, **kwargs):
        command = msg.content
        player = kwargs['player']
        print(f'\n!close command from {player}:\n{command}')

        try:
            qnumber = int(command.split(' ')[1])
        except (IndexError, ValueError):
            await msg.channel.send(t("wrong_close_usage"))
            return

        try:
            queue = LadderQueue.objects.get(id=qnumber)
        except LadderQueue.DoesNotExist:
            await msg.channel.send(t("wrong_close_usage"))
            return

        queue.active = False
        if queue.game_start_time:  # queue that is stuck in the game
            queue.game_end_time = timezone.now()
        queue.save()

        await self.queues_show()
        await msg.channel.send(t("queue_close").format(qnumber))

    async def player_join_queue(self, player, channel):
        # check if player is banned
        if player.banned:
            response = t("banned").format(player)
            return None, False, response

        # check if player is vouched
        if not player.vouched:
            response = t("not_vouched").format(player)
            return None, False, response

        # check if player has enough MMR
        if player.filter_mmr < channel.min_mmr:
            response = t("mmr_too_low").format(player)
            return None, False, response

        # check if player's mmr does not exceed limit, if there's any
        if player.filter_mmr > channel.max_mmr > 0:
            response = t("mmr_too_big").format(player)
            return None, False, response

        queue = player.ladderqueue_set.filter(
            Q(active=True) |
            Q(game_start_time__isnull=False) & Q(game_end_time__isnull=True)
        ).first()

        if queue:
            # check that player is not in this queue already
            if queue.channel == channel:
                response = t("already_in_this_queue").format(player)
                return queue, False, response

            # check that player is not already in a full queue
            if queue.players.count() == 10:
                response = t("already_in_full_queue").format(player)
                return None, False, response

        # remove player from other queues
        QueuePlayer.objects\
            .filter(player=player, queue__active=True)\
            .exclude(queue__channel=channel)\
            .delete()

        queue = Command.add_player_to_queue(player, channel)

        response = t("joined_inhouse").format(player, queue.id)

        is_queue_full = False
        # TODO: this is a separate function
        if queue.players.count() == 10:
            is_queue_full = True
            Command.balance_queue(queue)  # todo move this to QueuePlayer signal
            # response = TRANSLATIONS[LANG]["balance_str"].format(Command.balance_str(queue.balance))

            mention_str = f' '.join(self.player_mention(p) for p in queue.players.all())
            # "proposed_balance": "Kolejka jest pełna\n{}\n{}\nMasz {} min aby dołączyć do poczekalni",
            #   "balance_str": "Proponowany balans:\n{}",
            emoji = discord.utils.get(self.bot.emojis, id=968636489271476224) # Akek
            if not emoji:
                emoji = discord.utils.get(self.bot.emojis, id=1224958910599925785)  # dotaCraft hole fallback

            finalize = t("proposed_balance").format(queue.id, emoji, mention_str, WAITING_TIME_MINS)
            response = t("joined_inhouse").format(player, queue.id)

            # await self.queues_channel.send(finalize)
            await self.chat_channel.send(finalize)

        return queue, True, response

    @staticmethod
    def add_player_to_queue(player, channel):
        # TODO: this whole function should be QueueManager.add_player_to_queue()
        # get an available active queue
        queue = LadderQueue.objects\
            .filter(active=True)\
            .annotate(Count('players'))\
            .filter(players__count__lt=10, channel=channel)\
            .order_by('-players__count')\
            .first()

        if not queue:
            queue = LadderQueue.objects.create(
                min_mmr=channel.min_mmr,  # todo this should be done automatically when saving a new queue instance
                max_mmr=channel.max_mmr,
                channel=channel
            )

        # add player to the queue
        QueuePlayer.objects.create(
            queue=queue,
            player=player
        )

        return queue

    @staticmethod
    def balance_queue(queue):
        players = list(queue.players.all())
        result = BalanceResultManager.balance_teams(players)

        queue.balance = result.answers.first()
        queue.save()

    @staticmethod
    def balance_str(balance: BalanceAnswer, verbose=True):
        host = os.environ.get('BASE_URL', 'localhost:8000')
        url = reverse('balancer:balancer-answer', args=(balance.id,))
        url = '%s%s' % (host, url)

        # find out who's undergdog
        teams = balance.teams
        underdog = None
        if teams[1]['mmr'] - teams[0]['mmr'] >= MatchManager.underdog_diff:
            underdog = 0
        elif teams[0]['mmr'] - teams[1]['mmr'] >= MatchManager.underdog_diff:
            underdog = 1

        result = '```\n'
        for i, team in enumerate(balance.teams):
            if 'role_score_sum' in team:
                # this is balance with roles
                player_names = [f'{i+1}. {p[0]}' for i, p in enumerate(team['players'])]
            else:
                # balance without roles
                player_names = [p[0] for p in team['players']]

            team_name = "Radiant" if i == 0 else "Dire"
            result += f'{team_name} {"🐶" if i == underdog else " "} ' \
                      f'(śr. {team["mmr"]}): ' \
                      f'{" | ".join(player_names)}\n'

        if verbose:
            result += '\nLadder MMR: \n'
            for i, team in enumerate(balance.teams):
                player_mmrs = [str(p[1]) for p in team['players']]
                team_name = "Radiant" if i == 0 else "Dire"

                result += f'{team_name} {"🐶" if i == underdog else " "} ' \
                          f'(avg. {team["mmr"]}): ' \
                          f'{" | ".join(player_mmrs)}\n'

        result += f'\n{url}'
        result += '```'

        return result

    @staticmethod
    def get_teams_from_queue(q):
        answer = q.balance

        radiant_team = answer.teams[0]
        dire_team = answer.teams[1]

        radiant_avg_mmr = str(radiant_team['mmr'])
        dire_avg_mmr = str(dire_team['mmr'])

        radiant_player_ids = [p[0] for p in radiant_team['players']]
        dire_player_ids = [p[0] for p in dire_team['players']]

        radiant_players = Player.objects.filter(name__in=radiant_player_ids)
        dire_players = Player.objects.filter(name__in=dire_player_ids)

        return radiant_players, dire_players, radiant_avg_mmr, dire_avg_mmr

    @staticmethod
    def queue_str(q: LadderQueue, show_min_mmr=True):
        players = q.players.all()
        avg_mmr = round(mean(p.ladder_mmr for p in players))

        game_str = ''

        if players.count() == 10:
            radiant, dire, radiant_mmr, dire_mmr = Command.get_teams_from_queue(q)
            _radiant = [(p.name, p.ladder_mmr) for p in radiant]
            _dire = [(p.name, p.ladder_mmr) for p in dire]
            radiant_str = "\n".join([f'{i+1}. ' + "{:<15}".format(f'[#{p.rank_score}][{p.ladder_mmr}]') + f'<{p.name}>' for i, p in enumerate(radiant)])
            dire_str = "\n".join([f'{i+1}. ' + "{:<15}".format(f'[#{p.rank_score}][{p.ladder_mmr}]') + f'<{p.name}>' for i, p in enumerate(dire)])
            time_game = t("please_join_lobby")
            if q.game_start_time:
                time_difference_in_seconds = datetime.timestamp(timezone.now()) - datetime.timestamp(q.game_start_time)
                time_game = "od: " + str(round(time_difference_in_seconds / 60)) + " min"

            return t("game_start").format(q.id, time_game, radiant_mmr, radiant_str, dire_mmr, dire_str, q.id, q.game_server)

        return t("queue_str").format(
            q.id,
            avg_mmr,
            "\n".join([f'{i + 1}. ' + "{:<15}".format(f'[#{p.rank_score}][{p.ladder_mmr}]') + f'<{p.name}>' for i, p in
                       enumerate(players)])
        )


    @staticmethod
    def roles_str(roles: RolesPreference):
        return f'carry: {roles.carry} | mid: {roles.mid} | off: {roles.offlane} | ' + \
               f'pos4: {roles.pos4} | pos5: {roles.pos5}'

    @staticmethod
    def get_player_by_name(name):
        # check if name is a mention
        match = re.match(r'<@!?([0-9]+)>$', name)
        if match:
            return Player.objects.filter(discord_id=match.group(1)).first()

        # not a mention, proceed normally
        player = Player.objects.filter(name__iexact=name).first()

        # if exact match not found, try to guess player name
        if not player:
            player = Player.objects.filter(name__istartswith=name).first()
        if not player:
            player = Player.objects.filter(name__contains=name).first()

        return player

    def queue_full_msg(self, queue, show_balance=True):
        balance_str = ''
        auto_balance = LadderSettings.get_solo().draft_mode == LadderSettings.AUTO_BALANCE
        if auto_balance and show_balance:
            balance_str = t("balance_str").format(Command.balance_str(queue.balance))

        return t("proposed_balance").format(balance_str, f' '.join(self.player_mention(p) for p in queue.players.all()), WAITING_TIME_MINS)

    def player_mention(self, player):
        discord_id = int(player.discord_id) if player.discord_id else 0
        player_discord = self.bot.get_user(discord_id)
        mention = player_discord.mention if player_discord else player.name

        return mention

    def unregistered_mention(self, discord_user):
        user_discord = self.bot.get_user(discord_user.id)
        mention = user_discord.mention if user_discord else discord_user.name

        return mention

    async def channel_check_afk(self, channel: TextChannel, players):
        def last_seen(p):
            return self.last_seen[int(p.discord_id or 0)]

        def afk_filter(players, allowed_time):
            t = timedelta(minutes=allowed_time)
            afk = [p for p in players if timezone.now() - last_seen(p) > t]
            return afk

        afk_allowed_time = LadderSettings.get_solo().afk_allowed_time

        afk_list = afk_filter(players, afk_allowed_time)
        if not afk_list:
            return

        # for now, send afk pings in chat channel
        channel = DiscordChannels.get_solo().chat
        channel = self.bot.get_channel(channel)

        ping_list = [p for p in afk_list if p.queue_afk_ping]
        if ping_list:
            afk_response_time = LadderSettings.get_solo().afk_response_time

            msg = await channel.send(t("afk_check").format(" ".join(self.player_mention(p) for p in ping_list), afk_response_time))
            await msg.add_reaction('👌')
            await asyncio.sleep(afk_response_time * 60)

            # players who not responded
            afk_list = afk_filter(afk_list, afk_response_time)

        if not afk_list:
            return

        deleted, _ = QueuePlayer.objects\
            .filter(player__in=afk_list, queue__active=True)\
            .annotate(Count('queue__players'))\
            .filter(queue__players__count__lt=10)\
            .delete()

        if deleted > 0:
            await self.queues_show()
            await channel.send(t("purge").format(' | '.join(p.name for p in afk_list)))


    async def purge_queue_channels(self):
        channel = self.queues_channel
        await channel.purge()

    async def setup_queue_messages(self):
        channel = self.queues_channel

        # remove all Queue messages. The channel is closed, no other msgs should be around.
        db_messages = QueueChannel.objects.filter(active=True).values_list('discord_msg', flat=True)
        for msg in db_messages:
            try:
                msg = await channel.fetch_message(msg)
                await msg.delete()
            except:
                print("No message to delete")

        print("Purged queue messages")

        # recreate queues messages
        for q_type in QueueChannel.objects.filter(active=True):
            msg, created = await self.get_or_create_message(self.queues_channel, q_type.discord_msg)
            self.queue_messages[msg.id] = msg
            if created:
                q_type.discord_msg = msg.id
                q_type.save()

        await self.queues_show()

    async def queues_show(self):
        # remember queued players to check for changes in periodic task
        queued_players = [qp for qp in QueuePlayer.objects.filter(queue__active=True)]
        self.queued_players = set(qp.player.discord_id for qp in queued_players)
        self.last_queues_update = timezone.now()

        # show queues info
        for q_type in QueueChannel.objects.filter(active=True):
            message = self.queue_messages[q_type.discord_msg]

            min_mmr_string = f'({q_type.min_mmr}+)' if q_type.min_mmr > 0 else ''
            max_mmr_string = f'({q_type.max_mmr}-)' if q_type.max_mmr > 0 else ''
            queues = LadderQueue.objects\
                .filter(channel=q_type)\
                .filter(Q(active=True) |
                        Q(game_start_time__isnull=False) & Q(game_end_time__isnull=True))

            queues_text = t("no_queue")
            if queues:
                queues_text =  f'\n'.join(self.show_queue(q) for q in queues)

            text = f'**{q_type.name}** {min_mmr_string} {max_mmr_string}\n' + \
                   f'{queues_text}'

            try:
                await message.edit(content=text)

            except Exception as e:
                print(e)

            await self.attach_join_buttons_to_queue_msg(message)

    @staticmethod
    async def get_or_create_message(channel, msg_id):
        try:
            msg = await channel.fetch_message(msg_id)
            created = False
        except (DiscordPoll.DoesNotExist, discord.NotFound, discord.HTTPException):
            msg = await channel.send('.')
            created = True

        return msg, created

    async def update_status_message(self, text):
        channel = DiscordChannels.get_solo().queues
        channel = self.bot.get_channel(channel)

        event_time = timezone.localtime(timezone.now(), pytz.timezone('CET'))
        text = text.replace('`', '').replace('\n', '')  # remove unnecessary formatting
        self.status_responses.append(f'{event_time.strftime("%H:%M %Z"):<15}{text}')

        text = '```\n' + \
               '\n'.join(self.status_responses) + \
               '\n```'

        try:
            status_msg = discord.utils.get(self.bot.cached_messages, id=self.status_message)
            await status_msg.edit(content=text)
        except (DiscordPoll.DoesNotExist, discord.NotFound, discord.HTTPException, AttributeError):
            msg = await channel.send(text)
            self.status_message = msg.id

    def show_queue(self, q):
        q_string = self.queue_str(q, show_min_mmr=False)

        if q.players.count() == 10:
            auto_balance = LadderSettings.get_solo().draft_mode == LadderSettings.AUTO_BALANCE
            # if auto_balance:
                # q_string += self.balance_str(q.balance, verbose=q.active) + '\n'

        return q_string

    async def player_vouched(self, player):
        player.vouched = True
        player.save()

    async def player_leave_queue(self, player, msg):
        qs = QueuePlayer.objects \
            .filter(player=player, queue__active=True) \
            .select_related('queue') \
            .annotate(players_in_queue=Count('queue__players'))

        full_queue = next((q for q in qs if q.players_in_queue == 10), None)

        if full_queue:
            return t("in_game").format(player.name, full_queue.queue.id)

        deleted, _ = qs.delete()
        if deleted > 0:
            return t("player_queue_leave").format(player.name)
        else:
            return t("not_in_this_queue").format(player.name)

    async def handle_report_tip_command(self, msg, is_tip: bool, **kwargs):
        max_length = 160
        # Prevent bot from responding to its own messages
        if msg.author == self.bot.user:
            return

        # Check if the message is a private message
        if not msg.guild is None and is_tip == False:
            await msg.channel.send(t("report_in_dm"))
            return

        parts = msg.content.split()
        if len(parts) < 2:
            await msg.channel.send(t("report_tip_format"))
            return

        # Assume the command format is "!command ReportedPlayer [MatchID] Comment"
        reported_name = parts[1]
        match_id = None
        comment_index = 2

        # If the fourth part is a digit, it's considered a MatchID, and the comment starts from the fourth part
        if len(parts) > 2 and parts[2].isdigit():
            match_id = parts[2]
            comment_index = 3

        comment = " ".join(parts[comment_index:])

        try:
            # Fetch reporter and reported players based on Discord IDs (or names, depending on your setup)
            reporter = Player.objects.get(discord_id=msg.author.id)
            reported = Command.get_player_by_name(reported_name)

            if reporter == reported:
                await msg.channel.send(t("report_cannot_self"))
                return

            if not re.fullmatch(escaping_pattern, comment) or len(comment) > max_length:
                await msg.channel.send(t("invalid_report_comment").format(self.player_mention(reporter)))
                return

            # Call the appropriate method from ReportTipCommands
            if is_tip:
                result = self.report_tip_commands.tip_player_command(reporter, reported, match_id, comment)
            else:
                result = self.report_tip_commands.report_player_command(reporter, reported, match_id, comment)

            if isinstance(result, PlayerReport):
                # Determine the message format based on the value of the result
                if result.value > 0:
                    message_template = t("tip_success_msg")
                else:
                    message_template = t("report_success_msg")

                # Construct the custom success message using data from the PlayerReport instance
                custom_message = message_template.format(
                    self.player_mention(result.from_player),  # Reporter's or tipper's name
                    self.player_mention(result.to_player),  # Reported or tipped player's name
                    result.match.dota_id,  # Match ID, or indicate it's the last match
                    f"`{result.comment}`"  # Comment"
                )
                await msg.channel.send(custom_message)
            else:
                # The result is a string containing an error message
                await msg.channel.send(result)

        except Player.DoesNotExist:
            await msg.channel.send(t("report_could_not_find_players"))
        except Exception as e:
            print(str(e))
            await msg.channel.send("Error occurred in the bot script.")

    def get_help_commands(self):
        return {
            'Basic': {
                '!help': t("!help"),
                '!jak/!info': t("!jak/!info"),
                '!r/!reg': t("!r/!reg"),
                '!register': t("!register"),
                '!wh/!who/!whois/!profile/!stats': t("!wh/!who/!whois/!profile/!stats"),
                '!top': t("!top"),
                '!bot/!bottom': t("!bot/!bottom"),
                '!role/!roles': t("!r/!reg"),
                '!recent': t("!recent"),
                '!report': t("!report"),
                '!tip': t("!tip"),
                '!reports': t("!reports"),
                '!tips': t("!tips"),
            },
            'Queue': {
                '!vk/!votekick': t("!vk/!votekick"),
            }
        }

    def get_admin_help_commands(self):
        return  {
            'Admin': {
                '!vouch': t("!vouch"),
                '!ban': t("!ban"),
                '!unban': t("!unban"),
                '!set-mmr': t("!set-mmr/!adjust"),
                '!set-dota-id': t("!set-dota-id"),
            },
            'AdminQueue': {
                '!close': t("!close"),
                '!record-match': t("!record-match"),
                '!record-queue': t("!record-queue"),
                '!set-name/!rename': t("!set-name/!rename"),
            }
        }

    def get_available_bot_commands(self):
        return {
            '!register': self.register_command,
            '!vouch': self.vouch_command,
            '!wh': self.whois_command,
            '!who': self.whois_command,
            '!whois': self.whois_command,
            '!profile': self.whois_command,
            '!ban': self.ban_command,
            '!unban': self.unban_command,
            '!stats': self.whois_command,
            '!add': self.add_to_queue_command,
            '!kick': self.kick_from_queue_command,
            '!votekick': self.votekick_command,
            '!vk': self.votekick_command,
            '!mmr': self.mmr_command,
            '!top': self.top_command,
            '!bot': self.bottom_command,
            '!bottom': self.bottom_command,
            '!role': self.role_command,
            '!roles': self.role_command,
            '!recent': self.recent_matches_command,
            '!set-name': self.set_name_command,
            '!rename': self.rename_myself_command,
            '!set-mmr': self.set_mmr_command,
            '!adjust': self.set_mmr_command,
            '!set-dota-id': self.set_dota_id_command,
            '!my-dota-id': self.change_self_dota_id,
            '!record-match': self.record_match_command,
            '!record-queue': self.record_match_from_queue,
            '!help': self.help_command,
            '!adminhelp': self.admin_help_command,
            '!close': self.close_queue_command,
            '!reg': self.attach_help_buttons_to_msg,
            '!r': self.attach_help_buttons_to_msg,
            '!jak': self.registration_help_command,
            '!info': self.registration_help_command,
            '!report': lambda msg, **kwargs: self.handle_report_tip_command(msg, is_tip=False, **kwargs),
            '!tip': lambda msg, **kwargs: self.handle_report_tip_command(msg, is_tip=True, **kwargs),
            '!tips': self.handle_show_tips_command,
            '!reports': self.handle_show_reports_command,
        }

