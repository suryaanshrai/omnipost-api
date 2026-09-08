"""Runs the reconciler continuously. Intended as the `scheduler` process
(OMNIPOST_ROLE=scheduler in docker/entrypoint.sh) — a separate container from
`web` and `worker` so a slow request or a busy worker never delays recovery
of lost scheduled work."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from jobs.reconciler import reconcile_once

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Continuously reconcile scheduled PostTargets/PublishAttempts against the queue."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60, help="Seconds between sweeps.")
        parser.add_argument("--once", action="store_true", help="Run a single sweep and exit (for tests/cron).")

    def handle(self, *args, **options):
        interval = options["interval"]
        run_once = options["once"]
        while True:
            try:
                result = reconcile_once()
                if result["recovered_targets"] or result["requeued_attempts"]:
                    self.stdout.write(self.style.SUCCESS(f"Reconciled: {result}"))
            except Exception:  # noqa: BLE001 - never let one bad sweep kill the loop
                logger.exception("Reconciler sweep failed")
            if run_once:
                return
            time.sleep(interval)
