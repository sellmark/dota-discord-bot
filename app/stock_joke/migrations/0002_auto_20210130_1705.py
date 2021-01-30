# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2021-01-30 14:05
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stock_joke', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='stockjokesettings',
            name='discord_server_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='stockjokesettings',
            name='greed_role_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='stockjokesettings',
            name='red_role_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='stockjokesettings',
            name='stock_ticket',
            field=models.CharField(default='GME', max_length=200),
        ),
    ]
