PLAYER_NOT_FOUND_MESSAGE = "Nie znaleziono gracza, którego reportujesz."
MATCH_NOT_FOUND_MESSAGE = "Nie znaleziono meczu o ID: {}."
TIP_NO_MATCH_TO_REPORT = "Nie znaleziono meczu, do którego reportujesz."
NOT_PLAYED_TOGETHER = "Nie było Cię w grze {} z reportowanym graczem."
MATCH_TOO_OLD = "Mecz {} jest starszy niż 2 dni i nie można zgłaszać już reportów."
PLAYER_WITHOUT_GAMES = "Reportowany gracz nie ma żadnej rozegranej gry."
DUPLICATE_REPORT = "Istnieje już report/tip dla tej pary graczy w meczu {}."

from django.utils.timezone import now
from django.db.utils import IntegrityError
from app.ladder.models import Player, Match, PlayerReport, MatchPlayer  # Adjust the import path as necessary
from typing import Union

class ReportTipCommands:
    def process_command(self, reporter: Player, reported: Player, match_id: str = None, comment: str = '', is_tip: bool = False) -> Union[PlayerReport, str]:

        try:
            if match_id:
                match = Match.objects.get(dota_id=match_id)
            else:
                match_id = reporter.get_last_match_dota_id()
                if not match_id:
                    return TIP_NO_MATCH_TO_REPORT.format(reported.name)

                match = Match.objects.get(dota_id=match_id)

            if (now() - match.date).days > 2:
                return MATCH_TOO_OLD.format(match.dota_id)

            if not MatchPlayer.objects.filter(match=match, player=reporter).exists() or not MatchPlayer.objects.filter(match=match, player=reported).exists():
                return NOT_PLAYED_TOGETHER.format(match.dota_id)

            report_value = 1 if is_tip else -1
            report = PlayerReport(
                from_player=reporter,
                to_player=reported,
                match=match,
                reason='dump_this_column',
                comment=comment,
                value=report_value
            )
            report.save()
            return report
            # success_message = TIP_SUCCESS_MESSAGE if is_tip else REPORT_SUCCESS_MESSAGE
            # return success_message.format(reporter.name, reported.name, reason, match.id, comment)

        except Match.DoesNotExist:
            return MATCH_NOT_FOUND_MESSAGE.format(match_id)
        except IntegrityError:
            return DUPLICATE_REPORT.format(match_id)

    def report_player_command(self, reporter: Player, reported: Player,match_id: str = None, comment: str = '') -> Union[PlayerReport, str]:
        return self.process_command(reporter, reported, match_id, comment, is_tip=False)

    def tip_player_command(self, reporter: Player, reported: Player, match_id: str = None, comment: str = '') -> Union[PlayerReport, str]:
        return self.process_command(reporter, reported, match_id, comment, is_tip=True)