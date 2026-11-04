"""Public, unauthenticated pages: the link-in-bio page."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Post, Workspace


class BioPageView(APIView):
    """GET /public/bio/<slug>/: a workspace's link-in-bio page, if it has
    switched one on. Recent published posts, newest first, each pointing at
    its link (or its first published copy)."""

    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        responses=inline_serializer(
            "BioPage",
            fields={
                "name": serializers.CharField(),
                "description": serializers.CharField(),
                "links": serializers.ListField(child=serializers.DictField()),
                "posts": serializers.ListField(child=serializers.DictField()),
            },
        )
    )
    def get(self, request, slug: str):
        workspace = Workspace.objects.filter(slug=slug, bio_enabled=True).first()
        if workspace is None:
            raise NotFound()
        posts = (
            Post.objects.filter(workspace=workspace, status=Post.STATUS_PUBLISHED)
            .prefetch_related("targets")
            .order_by("-updated_at")[:20]
        )
        items = []
        for post in posts:
            published = [t for t in post.targets.all() if t.permalink]
            url = post.link or (published[0].permalink if published else "")
            if not url:
                continue
            items.append(
                {
                    "text": (post.base_text or "")[:280],
                    "url": url,
                    "published_at": max((t.published_at for t in published if t.published_at), default=None),
                }
            )
        return Response(
            {
                "name": workspace.name,
                "description": workspace.bio_description,
                "links": workspace.bio_links or [],
                "posts": items,
            }
        )
