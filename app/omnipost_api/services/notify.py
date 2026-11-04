"""Tell people when something they care about breaks: a post that failed to
publish, a channel that needs reconnecting.

Recipients are the post's author (when there is one) plus the workspace's
owners and admins. Each gets an in-app Notification; the email goes out from
a background job after the surrounding transaction commits.
"""

from __future__ import annotations

from django.db import transaction

from ..models import Membership, Notification, Post, User, Workspace


def _recipients(workspace: Workspace, post: Post | None) -> list[User]:
    users = {
        m.user_id: m.user
        for m in Membership.objects.filter(
            workspace=workspace, role__in=[Membership.ROLE_OWNER, Membership.ROLE_ADMIN]
        ).select_related("user")
    }
    author = post.author if post is not None else None
    if author is not None and author.pk not in users:
        if Membership.objects.filter(workspace=workspace, user=author).exists():
            users[author.pk] = author
    return list(users.values())


def notify(
    workspace: Workspace, kind: str, title: str, body: str = "", link: str = "", post: Post | None = None
) -> list[Notification]:
    created = [
        Notification.objects.create(user=user, workspace=workspace, kind=kind, title=title, body=body, link=link)
        for user in _recipients(workspace, post)
    ]
    ids = [n.pk for n in created if n.user.email and n.user.email_notifications]
    if ids:
        from jobs import queue as job_queue

        def send() -> None:
            for notification_id in ids:
                job_queue.enqueue_notification_email(notification_id)

        transaction.on_commit(send)
    return created


def post_failed(post_target) -> None:
    from jobs.webhooks import emit

    emit(post_target.post.workspace_id, "post.failed", _target_payload(post_target))
    post = post_target.post
    channel = post_target.channel
    detail = post_target.attempts.order_by("-created_at").values_list("error_detail", flat=True).first() or ""
    preview = (post.base_text or "").strip().replace("\n", " ")[:80]
    notify(
        post.workspace,
        Notification.KIND_POST_FAILED,
        title=f"A post to {channel.display_name} didn't go out",
        body=f"“{preview}”\n\n{detail}".strip(),
        link="/app",
        post=post,
    )


def channel_broken(channel) -> None:
    from jobs.webhooks import emit

    emit(
        channel.workspace_id,
        "channel.broken",
        {"channel": channel.pk, "connector": channel.connector_slug, "name": channel.display_name},
    )
    notify(
        channel.workspace,
        Notification.KIND_CHANNEL_BROKEN,
        title=f"{channel.display_name} needs reconnecting",
        body=channel.health_detail or "OmniPost can no longer post to this account until you reconnect it.",
        link=f"/app/connections/{channel.pk}",
    )


def preflight_failed(post_target) -> None:
    channel = post_target.channel
    notify(
        post_target.post.workspace,
        Notification.KIND_PREFLIGHT_FAILED,
        title=f"A scheduled post to {channel.display_name} may fail",
        body="The account isn't working right now. Reconnect it before the post's scheduled time.",
        link=f"/app/connections/{channel.pk}",
        post=post_target.post,
    )


def _target_payload(post_target) -> dict:
    return {
        "post": post_target.post_id,
        "target": post_target.pk,
        "channel": post_target.channel_id,
        "connector": post_target.channel.connector_slug,
        "status": post_target.status,
        "permalink": post_target.permalink,
        "text": post_target.post.base_text,
    }


def post_published(post_target) -> None:
    from jobs.webhooks import emit

    emit(post_target.post.workspace_id, "post.published", _target_payload(post_target))
