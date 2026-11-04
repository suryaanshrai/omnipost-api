"""OmniPost's data model.

Replaces the old schema entirely (six near-duplicate post tables — PostText,
PostImage, PostVideo, ShortFormVideo, StoryImage, StoryVideo — each with a
copy-pasted save() method; Platform.config as a hand-edited JSON blob;
PlatformInstance's password-derived credential encryption). There was no
production data to migrate (confirmed with the project owner), so this is a
clean break rather than a data migration.

The new shape is Organization -> Workspace -> Channel, with Post/PostTarget
splitting "the idea" from "what actually goes to each platform" — the
composer can now build one post with five platform-specific variants instead
of cross-posting identical text everywhere, which the old schema had no place
to put (post_configs existed but the UI never wrote to it).

The models are split by area; import them from here (omnipost_api.models).
"""

from .accounts import Membership, Organization, User, Workspace
from .activity import AuditEvent, Feed, Notification, Webhook
from .ai import ProviderKey, UsageCounter, VoiceProfile
from .channels import AppCredential, Channel, Credential
from .media import MediaAsset, MediaRendition
from .posts import Post, PostMetric, PostTarget, PostTargetPart, PublishAttempt
from .scheduling import BlackoutWindow, QueueSlot, RecurrenceRule

__all__ = [
    "AppCredential",
    "AuditEvent",
    "BlackoutWindow",
    "Channel",
    "Credential",
    "Feed",
    "MediaAsset",
    "MediaRendition",
    "Membership",
    "Notification",
    "Organization",
    "Post",
    "PostMetric",
    "PostTarget",
    "PostTargetPart",
    "ProviderKey",
    "PublishAttempt",
    "QueueSlot",
    "RecurrenceRule",
    "UsageCounter",
    "User",
    "VoiceProfile",
    "Webhook",
    "Workspace",
]
