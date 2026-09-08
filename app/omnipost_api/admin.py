from django.contrib import admin

from .models import (
    AppCredential,
    AuditEvent,
    BlackoutWindow,
    Channel,
    Credential,
    MediaAsset,
    MediaRendition,
    Membership,
    Organization,
    Post,
    PostMetric,
    PostTarget,
    PostTargetPart,
    ProviderKey,
    PublishAttempt,
    QueueSlot,
    RecurrenceRule,
    UsageCounter,
    User,
    VoiceProfile,
    Workspace,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "is_superuser", "first_name", "last_name", "email", "is_active", "date_joined")
    list_filter = ("last_login", "is_superuser", "is_staff", "is_active", "date_joined")
    raw_id_fields = ("groups", "user_permissions")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "plan", "created_at")
    list_filter = ("plan",)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "organization", "timezone", "created_at")
    list_filter = ("organization",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "workspace", "role", "created_at")
    list_filter = ("role", "workspace")


class ChannelCredentialInline(admin.StackedInline):
    model = Credential
    extra = 0
    readonly_fields = ("encrypted_values", "updated_at")
    can_delete = False


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("id", "display_name", "connector_slug", "workspace", "health", "token_expires_at")
    list_filter = ("connector_slug", "health", "workspace")
    inlines = [ChannelCredentialInline]


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "workspace", "size_bytes", "created_at")
    list_filter = ("kind", "workspace")


@admin.register(MediaRendition)
class MediaRenditionAdmin(admin.ModelAdmin):
    list_display = ("id", "asset", "profile", "width", "height")
    list_filter = ("profile",)


class PostTargetPartInline(admin.TabularInline):
    model = PostTargetPart
    extra = 0


class PostTargetInline(admin.TabularInline):
    model = PostTarget
    extra = 0
    readonly_fields = ("remote_id", "permalink", "status")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "author", "kind", "status", "scheduled_for", "created_at")
    list_filter = ("status", "kind", "workspace")
    date_hierarchy = "created_at"
    inlines = [PostTargetInline]


@admin.register(PostTarget)
class PostTargetAdmin(admin.ModelAdmin):
    list_display = ("id", "post", "channel", "status", "run_at", "remote_id")
    list_filter = ("status", "channel")
    inlines = [PostTargetPartInline]


@admin.register(PublishAttempt)
class PublishAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "post_target", "epoch", "attempt_number", "status", "error_class", "run_at")
    list_filter = ("status", "error_class")
    readonly_fields = ("state_data",)


@admin.register(PostMetric)
class PostMetricAdmin(admin.ModelAdmin):
    list_display = ("id", "post_target", "likes", "comments", "shares", "impressions", "fetched_at")
    readonly_fields = ("raw",)


@admin.register(VoiceProfile)
class VoiceProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "workspace", "is_default", "updated_at")
    list_filter = ("workspace",)


@admin.register(ProviderKey)
class ProviderKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "provider", "workspace", "is_active", "last_validated_at")
    list_filter = ("provider", "is_active")
    readonly_fields = ("encrypted_key",)


@admin.register(AppCredential)
class AppCredentialAdmin(admin.ModelAdmin):
    list_display = ("id", "connector_slug", "workspace", "client_id", "created_at")
    list_filter = ("connector_slug", "workspace")
    readonly_fields = ("encrypted_client_secret",)


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "feature", "period", "committed", "reserved", "limit")
    list_filter = ("feature", "period")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("id", "verb", "actor", "workspace", "created_at")
    list_filter = ("verb", "workspace")
    date_hierarchy = "created_at"


@admin.register(QueueSlot)
class QueueSlotAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "weekday", "time_of_day", "is_active")
    list_filter = ("channel", "weekday")


@admin.register(BlackoutWindow)
class BlackoutWindowAdmin(admin.ModelAdmin):
    list_display = ("id", "label", "workspace", "channel", "starts_at", "ends_at", "is_active")
    list_filter = ("workspace", "is_active")


@admin.register(RecurrenceRule)
class RecurrenceRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "interval_hours", "next_run_at", "is_active")
    list_filter = ("is_active", "channel")
