"""A small client for the OmniPost REST API, authenticated with a personal
API token (Settings -> API tokens)."""

from __future__ import annotations

import os
from typing import Any

import httpx


class OmniPostError(Exception):
    """The API refused a request; the message is the API's reason."""


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        if "detail" in body:
            return str(body["detail"])
        return "; ".join(f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in body.items())
    if isinstance(body, list):
        return " ".join(map(str, body))
    return f"HTTP {response.status_code}"


class OmniPostClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> OmniPostClient:
        token = os.environ.get("OMNIPOST_TOKEN")
        if not token:
            raise SystemExit("Set OMNIPOST_TOKEN to a personal API token (OmniPost: Settings -> API tokens).")
        return cls(os.environ.get("OMNIPOST_API_URL", "http://localhost:8000"), token)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise OmniPostError(_reason(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, json=body or {})

    def results(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """A list endpoint's items, paginated or not."""
        data = self.get(path, **params)
        return data["results"] if isinstance(data, dict) and "results" in data else data
