from django.contrib import admin

from .models import Platform, PlatformInstance, PostImage

@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    pass

@admin.register(PlatformInstance)
class PlatformInstanceAdmin(admin.ModelAdmin):
    pass

@admin.register(PostImage)
class PostImageAdmin(admin.ModelAdmin):
    pass