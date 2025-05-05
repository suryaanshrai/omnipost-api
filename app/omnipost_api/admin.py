from .models import *
from django import forms
from django.contrib import admin

class PlatformInstanceAdminForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=True,
        help_text="Password to encrypt the credentials. This password will not be stored in the database."
    )

    class Meta:
        model = PlatformInstance
        fields = '__all__'
    

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        'username',
        'is_superuser',
        'first_name',
        'last_name',
        'email',
        'is_active',
        'date_joined',
    )
    list_filter = (
        'last_login',
        'is_superuser',
        'is_staff',
        'is_active',
        'date_joined',
    )
    raw_id_fields = ('groups', 'user_permissions')


@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


@admin.register(PlatformInstance)
class PlatformInstanceAdmin(admin.ModelAdmin):
    list_display = ('id', 'platform', 'user')
    list_filter = ('platform', 'user')


@admin.register(PostText)
class PostTextAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'


@admin.register(PostImage)
class PostImageAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'


@admin.register(PostVideo)
class PostVideoAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
        'caption',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'


@admin.register(ShortFormVideo)
class ShortFormVideoAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'


@admin.register(StoryImage)
class StoryImageAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'

@admin.register(StoryVideo)
class StoryVideoAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'created_at',
        'schedule',
    )
    list_filter = ('user', 'created_at', 'schedule')
    raw_id_fields = ('platform_instances',)
    date_hierarchy = 'created_at'
    


@admin.register(Doc)
class DocAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'platform',
    )
    list_filter = ('platform',)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'platform_instance',
        'notification',
        'created_at',
        'error',
    )
    list_filter = ('platform_instance', 'user', 'created_at', 'error')
    date_hierarchy = 'created_at'