"""Runs the recurrence recycler continuously. Intended as its own process
(OMNIPOST_ROLE=recurrence in docker/entrypoint.sh) so a slow request or a
busy worker never delays an evergreen post's next occurrence — mirrors
reconcile_loop.py."""

from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from jobs.recurrence import run_due_recurrences

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Continuously fire due RecurrenceRules."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60, help="Seconds between sweeps.")
        parser.add_argument("--once", action="store_true", help="Run a single sweep and exit (for tests/cron).")

    def handle(self, *args, **options):
        interval = options["interval"]
        run_once = options["once"]
        while True:
            try:
                fired = run_due_recurrences()
                if fired:
                    self.stdout.write(self.style.SUCCESS(f"Fired {fired} due recurrence rule(s)"))
            except Exception:  # noqa: BLE001 - never let one bad sweep kill the loop
                logger.exception("Recurrence sweep failed")
            if run_once:
                return
            time.sleep(interval)
