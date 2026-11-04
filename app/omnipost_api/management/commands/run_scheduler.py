"""Runs every periodic task in one process: reconcile lost/stuck publishes,
fire recurrence rules, refresh expiring tokens and queue metrics polls.

Replaces four near-identical loop commands, each its own container (about
100 MB of RAM apiece on a small VPS). A task that raises is logged and
retried at its next interval; it never stops the others.

`--only <task>` runs a subset (the old per-task roles map onto this), and
`--once` runs each selected task one time and exits (cron, tests).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


@dataclass
class Task:
    name: str
    interval_s: int
    run: Callable[[], object]


def _tasks() -> list[Task]:
    from jobs.health import refresh_expiring_channels
    from jobs.metrics import sweep_due_metrics
    from jobs.reconciler import reconcile_once
    from jobs.recurrence import run_due_recurrences

    return [
        Task("reconcile", 60, reconcile_once),
        Task("recurrence", 60, run_due_recurrences),
        Task("token-refresh", 900, refresh_expiring_channels),
        Task("metrics", 1800, sweep_due_metrics),
    ]


class Command(BaseCommand):
    help = "Run OmniPost's periodic tasks (reconcile, recurrence, token refresh, metrics)."

    def add_arguments(self, parser):
        parser.add_argument("--only", action="append", default=[], help="Task name; repeatable.")
        parser.add_argument("--once", action="store_true", help="Run each task once and exit.")

    def handle(self, *args, **options):
        tasks = _tasks()
        if options["only"]:
            unknown = set(options["only"]) - {t.name for t in tasks}
            if unknown:
                raise SystemExit(f"Unknown task(s): {', '.join(sorted(unknown))}")
            tasks = [t for t in tasks if t.name in options["only"]]

        next_run = {t.name: 0.0 for t in tasks}
        while True:
            now = time.monotonic()
            for task in tasks:
                if now < next_run[task.name]:
                    continue
                try:
                    result = task.run()
                    # Only log runs that did something.
                    if result and (not isinstance(result, dict) or any(result.values())):
                        self.stdout.write(f"{task.name}: {result}")
                except Exception:  # noqa: BLE001 - one failing task must not stop the rest
                    logger.exception("Scheduled task %s failed", task.name)
                next_run[task.name] = now + task.interval_s
            if options["once"]:
                return
            time.sleep(max(1.0, min(next_run.values()) - time.monotonic()))
