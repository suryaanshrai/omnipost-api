# Generated by Django 5.1.7 on 2025-03-22 15:42

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnipost_api', '0002_alter_postimage_post_configs_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='postimage',
            name='post_configs',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
        migrations.AlterField(
            model_name='postvideo',
            name='post_configs',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
        migrations.AlterField(
            model_name='shortformvideo',
            name='post_configs',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
        migrations.AlterField(
            model_name='stories',
            name='post_configs',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
    ]
