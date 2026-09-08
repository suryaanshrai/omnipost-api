"""Liveness/readiness probes.

Replaces the docker-compose `test: "exit 0"` healthchecks, which always pass
and give `depends_on: condition: service_healthy` no real meaning.
"""

import redis
from django.conf import settings
from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.views.decorators.http import require_GET

# These are plain Django views, not DRF APIViews, so the project-wide
# REST_FRAMEWORK DEFAULT_PERMISSION_CLASSES (IsAuthenticated) never applies to
# them — they are public by construction, which is what a container
# healthcheck needs.


@require_GET
def healthz(request):
    """Process is up and can serve requests. No dependency checks."""
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(request):
    """Process can actually do its job: DB and the RQ Redis are reachable."""
    checks = {}

    try:
        connections["default"].cursor().execute("SELECT 1")
        checks["database"] = "ok"
    except OperationalError as exc:
        checks["database"] = f"error: {exc}"

    try:
        queue_cfg = settings.RQ_QUEUES["default"]
        client = redis.Redis(
            host=queue_cfg["HOST"],
            port=queue_cfg["PORT"],
            db=queue_cfg["DB"],
            password=queue_cfg.get("PASSWORD"),
            socket_connect_timeout=2,
        )
        client.ping()
        checks["redis"] = "ok"
    except redis.RedisError as exc:
        checks["redis"] = f"error: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse({"status": "ok" if healthy else "unhealthy", "checks": checks}, status=200 if healthy else 503)
