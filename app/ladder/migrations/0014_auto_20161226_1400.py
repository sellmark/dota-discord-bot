# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-12-26 11:00
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ladder', '0013_auto_20161212_2243'),
    ]

    operations = [
        migrations.RenameField(
            model_name='scorechange',
            old_name='amount',
            new_name='score_change',
        ),
    ]
