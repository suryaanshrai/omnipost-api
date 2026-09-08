from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .services.provisioning import create_personal_workspace


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def provision_personal_workspace(sender, instance, created, **kwargs):
    if not created:
        return
    create_personal_workspace(instance)
