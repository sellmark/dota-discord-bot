# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2024-02-23 18:42
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('balancer', '0006_auto_20170109_1401'),
    ]

    operations = [
        migrations.AlterField(
            model_name='balanceanswer',
            name='mmr_diff',
            field=models.BigIntegerField(),
        ),
        migrations.AlterField(
            model_name='balanceanswer',
            name='mmr_diff_exp',
            field=models.BigIntegerField(),
        ),
    ]
