"""Runs the token-refresh sweep continuously — the proactive half of
Connection Health (see jobs/health.py). Intended as part of the `scheduler`
process alongside reconcile_loop."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from jobs.health import refresh_expiring_channels

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Continuously refresh channel tokens nearing expiry."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=900, help="Seconds between sweeps.")
        parser.add_argument("--once", action="store_true", help="Run a single sweep and exit.")

    def handle(self, *args, **options):
        interval = options["interval"]
        run_once = options["once"]
        while True:
            try:
                result = refresh_expiring_channels()
                if result["refreshed"] or result["broken"]:
                    self.stdout.write(self.style.SUCCESS(f"Token refresh: {result}"))
            except Exception:  # noqa: BLE001 - never let one bad sweep kill the loop
                logger.exception("Token refresh sweep failed")
            if run_once:
                return
            time.sleep(interval)
