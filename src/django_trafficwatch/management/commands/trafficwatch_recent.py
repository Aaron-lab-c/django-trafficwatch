"""List (or clear) the recent limit violations recorded by the middleware."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from django.core.cache import caches
from django.core.management.base import BaseCommand, CommandParser

from django_trafficwatch import stats
from django_trafficwatch.conf import tw_settings


class Command(BaseCommand):
    help = "Show recent traffic-limit violations kept in the cache (newest first)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "-n", "--limit", type=int, default=20, help="Rows to show (default 20)."
        )
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
        parser.add_argument("--clear", action="store_true", help="Delete the stored violations.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["clear"]:
            stats.clear_recent_violations()
            self.stdout.write(self.style.SUCCESS("Cleared recent violations."))
            return

        rows = stats.recent_violations(options["limit"])
        if not rows and "locmem" in type(caches[tw_settings.CACHE_ALIAS]).__name__.lower():
            self.stderr.write(
                f"Note: CACHES[{tw_settings.CACHE_ALIAS!r}] is LocMemCache, which lives inside "
                "each process. This command runs in its own process and cannot see what the "
                "web workers recorded; use a Redis or Memcached cache to share the data."
            )
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2, default=str))
            return
        if not rows:
            self.stdout.write("No violations recorded.")
            return

        header = (
            f"{'when (UTC)':20} {'client':28} {'rule':28} {'method':7} {'count/limit':12} "
            f"{'lock':8} path"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for row in rows:
            when = datetime.fromtimestamp(row.get("at", 0), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            client = str(row.get("client", ""))[:28]
            rule = str(row.get("rule", ""))[:28]
            ratio = f"{row.get('count', '?')}/{row.get('limit', '?')}"
            lockout = row.get("lockout") or {}
            if lockout.get("active"):
                lock = "LOCKED"
            elif lockout:
                lock = f"{lockout.get('violations', '?')}/{lockout.get('threshold', '?')}"
            else:
                lock = ""
            self.stdout.write(
                f"{when:20} {client:28} {rule:28} {str(row.get('method', '')):7} "
                f"{ratio:12} {lock:8} {row.get('path', '')}"
            )
