"""Delimited template substitution for declarative connector specs.

Replaces omnipost_api.models.replace_keys(), which did undelimited
`str.replace()` across an entire JSON-serialized request. That meant a
caption containing the literal substring "TEXT" or "POST_ID" silently
corrupted the outbound request, and a credential value containing a `"` broke
`json.loads()` outright. This engine:

  * only substitutes inside `{{ VAR_NAME }}` tokens, so plain user text is
    never touched no matter what it contains;
  * substitutes into the *parsed* structure (dict/list), never into a
    serialized JSON string, so there is no way for a value to break the
    surrounding syntax;
  * raises on an unknown variable instead of leaving `{{ VAR }}` in the
    request or silently dropping it;
  * preserves the value's type when a leaf is *exactly* one placeholder
    (`{{ MEDIA_IDS }}` can substitute a list), and falls back to string
    interpolation when a placeholder is embedded in a larger string
    (`"Bearer {{ ACCESS_TOKEN }}"`).
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


class TemplateError(ValueError):
    pass


class UnknownVariable(TemplateError):
    def __init__(self, name: str):
        super().__init__(f"Template references unknown variable: {name}")
        self.name = name


def render(node: Any, context: dict[str, Any]) -> Any:
    """Recursively render a parsed spec fragment (dict/list/str/other)
    against `context`. Non-string leaves pass through unchanged."""
    if isinstance(node, dict):
        return {key: render(value, context) for key, value in node.items()}
    if isinstance(node, list):
        return [render(item, context) for item in node]
    if isinstance(node, str):
        return _render_string(node, context)
    return node


def _render_string(text: str, context: dict[str, Any]) -> Any:
    full_match = _TOKEN_RE.fullmatch(text.strip())
    if full_match:
        name = full_match.group(1)
        if name not in context:
            raise UnknownVariable(name)
        return context[name]

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise UnknownVariable(name)
        return str(context[name])

    return _TOKEN_RE.sub(substitute, text)


def referenced_variables(node: Any) -> set[str]:
    """All variable names a spec fragment references. Used at spec-load time
    (sync_connectors management command) to validate that a spec doesn't
    reference a variable no earlier step or the INSTANCE schema provides."""
    found: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            found |= referenced_variables(value)
    elif isinstance(node, list):
        for item in node:
            found |= referenced_variables(item)
    elif isinstance(node, str):
        found |= {m.group(1) for m in _TOKEN_RE.finditer(node)}
    return found
