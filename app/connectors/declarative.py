"""Connector driven entirely by a YAML spec (see specs/*.yaml) — the
"new platform in an hour" path for simple, single/multi-request JSON REST
APIs with no media upload, chunked transfer, or async processing.

This is the direct descendant of the old Platform.config JSON-in-a-DB-column
idea, but: specs live in version control (not a hand-edited admin field),
are typed and validated at load time (validate_spec, called by the
sync_connectors management command and in CI), and are executed by the
delimited template engine instead of `replace_keys()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .base import Connector, PublishContext, PublishOutcome, PublishState
from .capabilities import Capabilities, MediaRule, RateLimit
from .errors import (
    AuthExpired,
    ConnectorError,
    DuplicatePost,
    ErrorClass,
    InvalidMedia,
    PermanentError,
    PolicyViolation,
    RateLimited,
    TransientError,
)
from .template import UnknownVariable, referenced_variables, render

_ERROR_CLASS_TO_EXCEPTION: dict[str, type[ConnectorError]] = {
    ErrorClass.RATE_LIMITED.value: RateLimited,
    ErrorClass.AUTH_EXPIRED.value: AuthExpired,
    ErrorClass.INVALID_MEDIA.value: InvalidMedia,
    ErrorClass.DUPLICATE.value: DuplicatePost,
    ErrorClass.POLICY_VIOLATION.value: PolicyViolation,
    ErrorClass.TRANSIENT.value: TransientError,
    ErrorClass.PERMANENT.value: PermanentError,
}


class SpecError(ValueError):
    """Raised when a spec is structurally invalid — caught at load time
    (sync_connectors / CI), never at publish time."""


def validate_spec(spec: dict[str, Any]) -> None:
    """Static checks a spec must pass before it's trusted to run. Doesn't
    catch everything (e.g. a wrong but well-formed endpoint), but catches the
    class of bug that made the old system's linkedin.json and facebook.json
    silently unusable: missing request envelopes, and variables no step or
    the credential schema actually provides."""
    for field_name in ("slug", "display_name", "instance", "actions"):
        if field_name not in spec:
            raise SpecError(f"spec missing required top-level field: {field_name}")

    known_vars = {"TEXT", "CAPTION", "LINK", *spec["instance"]}

    for action_name, action in spec["actions"].items():
        steps = action.get("steps")
        if not steps:
            raise SpecError(f"action '{action_name}' has no steps")
        available = set(known_vars)
        for i, step in enumerate(steps):
            request = step.get("request")
            if request is None:
                raise SpecError(f"action '{action_name}' step {i} missing 'request'")
            for required in ("method", "base_url", "endpoint"):
                if required not in request:
                    raise SpecError(f"action '{action_name}' step {i} request missing '{required}'")
            used = referenced_variables(request)
            unknown = used - available
            if unknown:
                raise SpecError(
                    f"action '{action_name}' step {i} references undefined variable(s): {sorted(unknown)}"
                )
            for produced in step.get("response_mapping", {}).values():
                available.add(produced)


def _capabilities_from_spec(spec: dict[str, Any]) -> Capabilities:
    caps = spec.get("capabilities", {})
    media = tuple(
        MediaRule(
            kinds=tuple(m.get("kinds", ())),
            max_count=m.get("max_count", 0),
            max_size_mb=m.get("max_size_mb"),
            max_duration_s=m.get("max_duration_s"),
            min_duration_s=m.get("min_duration_s"),
            allowed_aspect_ratios=tuple(m.get("allowed_aspect_ratios", ())),
            allowed_mime_types=tuple(m.get("allowed_mime_types", ())),
        )
        for m in caps.get("media", [])
    )
    rate_limits = tuple(
        RateLimit(max_requests=r["max_requests"], per_seconds=r["per_seconds"], scope=r.get("scope", "channel"))
        for r in caps.get("rate_limits", [])
    )
    return Capabilities(
        slug=spec["slug"],
        display_name=spec["display_name"],
        max_text_length=caps.get("max_text_length"),
        supports_link=caps.get("supports_link", True),
        supports_alt_text=caps.get("supports_alt_text", False),
        supports_threads=caps.get("supports_threads", False),
        supports_scheduling_native=caps.get("supports_scheduling_native", False),
        media=media,
        rate_limits=rate_limits,
        post_kinds=tuple(caps.get("post_kinds", ("text",))),
    )


@dataclass
class DeclarativeConnector(Connector):
    spec: dict[str, Any]

    def __post_init__(self) -> None:
        self.slug = self.spec["slug"]
        self.capabilities = _capabilities_from_spec(self.spec)
        # A spec's `instance` list is exactly the credential bag its steps
        # template against — the same keys a user pastes in to connect.
        self.credential_fields = tuple(self.spec["instance"])

    def _action_name_for(self, post_kind: str) -> str:
        mapping = {
            "text": "publish_text",
            "image": "publish_image",
            "video": "publish_video",
            "story": "publish_story",
            "short_video": "publish_short_video",
        }
        name = mapping.get(post_kind)
        if name is None or name not in self.spec["actions"]:
            raise PermanentError(f"{self.capabilities.display_name} has no '{post_kind}' action defined")
        return name

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        action = self.spec["actions"][self._action_name_for(ctx.post_kind)]
        steps = action["steps"]
        start_index = state.data.get("step_index", 0)
        variables: dict[str, Any] = dict(state.data.get("variables", {}))
        variables.setdefault("TEXT", ctx.text)
        variables.setdefault("CAPTION", ctx.text)
        variables.setdefault("LINK", ctx.link or "")
        variables.update(ctx.credentials.values)

        outcome_remote_id: str | None = state.data.get("remote_id")
        outcome_permalink: str | None = state.data.get("permalink")

        for index in range(start_index, len(steps)):
            step = steps[index]
            try:
                rendered = render(step["request"], variables)
            except UnknownVariable as exc:
                raise PermanentError(f"Spec error: {exc}") from exc

            response = requests.request(
                rendered["method"],
                rendered["base_url"].rstrip("/") + rendered["endpoint"],
                headers=rendered.get("headers") or {},
                params=rendered.get("params") or {},
                json=rendered.get("payload") if rendered.get("payload") is not None else None,
                timeout=30,
            )

            expected = step.get("expected_status", [200, 201])
            if response.status_code not in expected:
                self._raise_mapped_error(step, response)

            body = _safe_json(response)
            for response_key, variable_name in step.get("response_mapping", {}).items():
                variables[variable_name] = _dig(body, response_key)

            if step.get("terminal"):
                outcome_remote_id = variables.get("REMOTE_ID")
                outcome_permalink = variables.get("PERMALINK")

        return PublishOutcome(done=True, remote_id=outcome_remote_id, permalink=outcome_permalink)

    def _raise_mapped_error(self, step: dict[str, Any], response: requests.Response) -> None:
        error_map = step.get("error_map", {})
        class_name = error_map.get(response.status_code, ErrorClass.PERMANENT.value)
        exc_type = _ERROR_CLASS_TO_EXCEPTION.get(class_name, PermanentError)
        retry_after = None
        if exc_type is RateLimited:
            header = response.headers.get("Retry-After")
            retry_after = float(header) if header and header.isdigit() else 30.0
        raise exc_type(
            f"{self.capabilities.display_name} returned {response.status_code}",
            raw_detail=response.text[:2000],
            retry_after=retry_after,
        )


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _dig(body: Any, dotted_key: str) -> Any:
    value = body
    for part in dotted_key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value
