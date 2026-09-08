"""Runs jobs.metrics.sweep_due_metrics continuously. Intended as its own
process (OMNIPOST_ROLE=metrics-poller in docker/entrypoint.sh), mirroring
reconcile_loop.py/recur_loop.py — a slow or rate-limited metrics API should
never delay publishing or reconciliation."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from jobs.metrics import sweep_due_metrics

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Continuously enqueue metrics polls for published PostTargets due for a refresh."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=1800, help="Seconds between sweeps.")
        parser.add_argument("--once", action="store_true", help="Run a single sweep and exit (for tests/cron).")

    def handle(self, *args, **options):
        interval = options["interval"]
        run_once = options["once"]
        while True:
            try:
                count = sweep_due_metrics()
                if count:
                    self.stdout.write(self.style.SUCCESS(f"Enqueued {count} metrics poll(s)"))
            except Exception:  # noqa: BLE001 - never let one bad sweep kill the loop
                logger.exception("Metrics sweep failed")
            if run_once:
                return
            time.sleep(interval)
