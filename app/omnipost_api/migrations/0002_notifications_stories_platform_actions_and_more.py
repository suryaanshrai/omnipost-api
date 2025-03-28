# Generated by Django 5.1.7 on 2025-03-09 18:37

import django.db.models.deletion
import omnipost_api.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnipost_api', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Notifications',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
        ),
        migrations.CreateModel(
            name='Stories',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
        ),
        migrations.AddField(
            model_name='platform',
            name='actions',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='platform',
            name='configs',
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name='platform',
            name='name',
            field=models.CharField(default='', max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platforminstance',
            name='credentials',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='platforminstance',
            name='platform',
            field=models.ForeignKey(default=0, on_delete=django.db.models.deletion.CASCADE, to='omnipost_api.platform'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='platforminstance',
            name='user',
            field=models.ForeignKey(default=0, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='post',
            name='content',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='post',
            name='media',
            field=models.FileField(blank=True, null=True, upload_to='media/', validators=[omnipost_api.models.Post.validate_media_file]),
        ),
        migrations.AddField(
            model_name='post',
            name='post_configs',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='post',
            name='user',
            field=models.ForeignKey(default=0, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name='Docs',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('remote_doc', models.URLField(blank=True, null=True)),
                ('custom_doc', models.TextField(blank=True, null=True)),
                ('platform', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='omnipost_api.platform')),
                ('platform_instance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='omnipost_api.platforminstance')),
            ],
        ),
    ]
