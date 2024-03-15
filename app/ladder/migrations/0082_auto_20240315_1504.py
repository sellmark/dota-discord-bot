# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2024-03-15 15:04
from __future__ import unicode_literals

import datetime
from django.db import migrations, models
from django.utils.timezone import utc
import multiselectfield.db.fields


class Migration(migrations.Migration):

    dependencies = [
        ('ladder', '0081_auto_20240315_1440'),
    ]

    operations = [
        migrations.AddField(
            model_name='playerreport',
            name='report_date',
            field=models.DateTimeField(auto_now_add=True, default=datetime.datetime(2024, 3, 15, 15, 4, 14, 121957, tzinfo=utc)),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='queuechannel',
            name='active_on',
            field=multiselectfield.db.fields.MultiSelectField(blank=True, choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')], default=(0, 1, 2, 3, 4, 5, 6), max_length=13, null=True),
        ),
    ]
