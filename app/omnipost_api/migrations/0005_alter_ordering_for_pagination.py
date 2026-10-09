# Adds an explicit `ordering` to every model paginated by
# LimitOffsetPagination that lacked one. Without it, Postgres has no
# deterministic tie-break for equal-cost rows and offset pagination across
# such a queryset returns duplicates and gaps nondeterministically as rows
# are inserted between page fetches. No column or index changes — options
# only.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnipost_api', '0004_posttarget_published_at_postmetric'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='channel',
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['workspace', 'connector_slug'], name='omnipost_ap_workspa_ccafec_idx'),
                ],
            },
        ),
        migrations.AlterModelOptions(
            name='mediaasset',
            options={'ordering': ['-created_at']},
        ),
        migrations.AlterModelOptions(
            name='post',
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['workspace', 'status'], name='omnipost_ap_workspa_cb13e3_idx')],
            },
        ),
        migrations.AlterModelOptions(
            name='posttarget',
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['channel', 'status', 'run_at'], name='omnipost_ap_channel_e852e6_idx'),
                ],
            },
        ),
        migrations.AlterModelOptions(
            name='publishattempt',
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['status', 'run_at'], name='omnipost_ap_status_8c745f_idx')],
                'unique_together': {('post_target', 'epoch', 'attempt_number')},
            },
        ),
    ]
