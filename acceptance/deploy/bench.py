"""Throughput / latency of the middleware under a multi-worker server, per backend.

    TW_REDIS_URL=redis://localhost:6379/4 TW_MEMCACHED=localhost:11211 \\
        python -m deploy.bench [--workers 4] [--threads 16] [--requests 200] [--server gunicorn]

Compares an exempt path (no counting at all) with a counted path for every backend/store
combination, so the cost of the middleware itself is visible.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deploy.harness import Server, blast  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--requests", type=int, default=200, help="per thread")
    ap.add_argument("--server", choices=["gunicorn", "uvicorn"], default="gunicorn")
    args = ap.parse_args()

    stores = []
    if os.environ.get("TW_REDIS_URL"):
        stores.append(("redis", {"TW_REDIS_URL": os.environ["TW_REDIS_URL"]}))
    if os.environ.get("TW_MEMCACHED"):
        stores.append(
            ("memcached", {"TW_MEMCACHED": os.environ["TW_MEMCACHED"], "TW_REDIS_URL": ""})
        )
    if not stores:
        sys.exit("set TW_REDIS_URL and/or TW_MEMCACHED")

    print(
        f"{args.server} x{args.workers} workers, {args.threads} threads x {args.requests} requests"
    )
    rows = []
    for store, env in stores:
        for backend in ("fixed", "sliding", "redis"):
            if backend == "redis" and store != "redis":
                continue
            with Server(args.server, args.workers, {**env, "TW_BACKEND": backend}) as srv:
                client = f"198.51.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}"
                blast(srv, "/health/", client, 4, 20)  # warm up workers
                base = blast(srv, "/health/", client, args.threads, args.requests)
                counted = blast(srv, "/bench-open/", client, args.threads, args.requests)
                rows.append((store, backend, base, counted))
                print(f"  {store:9} {backend:8} exempt : {base.summary()}")
                print(f"  {store:9} {backend:8} counted: {counted.summary()}")
    print()
    print(
        f"{'store':9} {'backend':8} {'exempt rps':>10} {'counted rps':>11} "
        f"{'p50 ms':>7} {'p99 ms':>7} {'overhead/req':>12}"
    )
    for store, backend, base, counted in rows:
        overhead = counted.pct(0.5) - base.pct(0.5)
        print(
            f"{store:9} {backend:8} {base.rps:10.0f} {counted.rps:11.0f} "
            f"{counted.pct(0.5):7.1f} {counted.pct(0.99):7.1f} {overhead:9.2f} ms"
        )


if __name__ == "__main__":
    main()
