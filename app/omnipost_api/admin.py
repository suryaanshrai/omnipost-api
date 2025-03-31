from django.contrib import admin
from django import forms
from .models import Platform, PlatformInstance, PostImage, ShortFormVideo, PostVideo, Stories

@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    pass

class PlatformInstanceAdminForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=True,
        help_text="Password to encrypt the credentials. This password will not be stored in the database."
    )

    class Meta:
        model = PlatformInstance
        fields = '__all__'
    
@admin.register(PlatformInstance)
class PlatformInstanceAdmin(admin.ModelAdmin):
    form = PlatformInstanceAdminForm
    
    def save_model(self, request, obj, form, change):
        password = form.cleaned_data.get('password')
        obj.save(password=password)


@admin.register(PostImage)
class PostImageAdmin(admin.ModelAdmin):
    pass

@admin.register(ShortFormVideo)
class ShortFormVideoAdmin(admin.ModelAdmin):
    pass

@admin.register(PostVideo)
class PostVideoAdmin(admin.ModelAdmin):
    pass

@admin.register(Stories)
class StoriesAdmin(admin.ModelAdmin):
    pass