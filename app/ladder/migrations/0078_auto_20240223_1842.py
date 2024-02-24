# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2024-02-23 18:42
from __future__ import unicode_literals

from django.db import migrations, models
import multiselectfield.db.fields


class Migration(migrations.Migration):

    dependencies = [
        ('ladder', '0077_auto_20240220_0629'),
    ]

    operations = [
        migrations.AlterField(
            model_name='laddersettings',
            name='afk_allowed_time',
            field=models.PositiveSmallIntegerField(default=25),
        ),
        migrations.AlterField(
            model_name='queuechannel',
            name='active_on',
            field=multiselectfield.db.fields.MultiSelectField(blank=True, choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')], default=(0, 1, 2, 3, 4, 5, 6), max_length=13, null=True),
        ),
    ]
