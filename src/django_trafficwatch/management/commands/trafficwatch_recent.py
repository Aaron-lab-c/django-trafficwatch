"""List (or clear) the recent limit violations recorded by the middleware."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from django_trafficwatch import stats


class Command(BaseCommand):
    help = "Show recent traffic-limit violations kept in the cache (newest first)."

    def add_arguments(self, parser):
        parser.add_argument(
            "-n", "--limit", type=int, default=20, help="Rows to show (default 20)."
        )
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
        parser.add_argument("--clear", action="store_true", help="Delete the stored violations.")

    def handle(self, *args, **options):
        if options["clear"]:
            stats.clear_recent_violations()
            self.stdout.write(self.style.SUCCESS("Cleared recent violations."))
            return

        rows = stats.recent_violations(options["limit"])
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2, default=str))
            return
        if not rows:
            self.stdout.write("No violations recorded.")
            return

        header = f"{'when (UTC)':20} {'client':28} {'rule':28} {'method':7} {'count/limit':12} path"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for row in rows:
            when = datetime.fromtimestamp(row.get("at", 0), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            client = str(row.get("client", ""))[:28]
            rule = str(row.get("rule", ""))[:28]
            ratio = f"{row.get('count', '?')}/{row.get('limit', '?')}"
            self.stdout.write(
                f"{when:20} {client:28} {rule:28} {str(row.get('method', '')):7} "
                f"{ratio:12} {row.get('path', '')}"
            )
