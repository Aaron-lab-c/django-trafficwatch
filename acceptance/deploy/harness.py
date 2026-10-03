"""Drive the acceptance project through a real multi-process server.

Starts gunicorn (WSGI) or uvicorn (ASGI) with N workers on a free port, with the counter
store and backend chosen through environment variables, then fires concurrent requests from
many threads. Client identity is chosen per scenario through ``X-Forwarded-For`` (loopback is
a trusted proxy in ``TW_DEPLOY`` mode), so scenarios never share counters and no cache flush
is needed between them.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ACCEPTANCE_DIR = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


@dataclass
class Server:
    kind: str  # "gunicorn" | "uvicorn"
    workers: int
    env: dict[str, str]
    port: int = 0
    proc: subprocess.Popen | None = None
    log: Path = field(default_factory=lambda: Path(os.environ.get("TW_DEPLOY_LOG", "/tmp")))

    def __enter__(self) -> Server:
        self.port = free_port()
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "project.settings",
            "PYTHONPATH": str(ACCEPTANCE_DIR),
            "TW_DEPLOY": "1",
            **self.env,
        }
        if self.kind == "gunicorn":
            cmd = [
                sys.executable,
                "-m",
                "gunicorn",
                "project.wsgi:application",
                "--workers",
                str(self.workers),
                "--bind",
                f"127.0.0.1:{self.port}",
                "--log-level",
                "warning",
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "project.asgi:application",
                "--workers",
                str(self.workers),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ]
        logfile = open(self.log / f"tw-{self.kind}-{self.port}.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=ACCEPTANCE_DIR, stdout=logfile, stderr=logfile
        )
        wait_for_port(self.port, self.proc)
        # uvicorn's workers may still be importing Django; make sure a request succeeds.
        for _ in range(50):
            try:
                self.get("/health/", "203.0.113.250")
                break
            except OSError:
                time.sleep(0.2)
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.proc is not None
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def get(self, path: str, client_ip: str, conn: http.client.HTTPConnection | None = None):
        own = conn is None
        conn = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"X-Forwarded-For": client_ip, "Host": "localhost"})
        resp = conn.getresponse()
        resp.read()
        if own:
            conn.close()
        return resp


@dataclass
class LoadResult:
    statuses: Counter
    latencies_ms: list[float]
    seconds: float

    @property
    def rps(self) -> float:
        return len(self.latencies_ms) / self.seconds

    def pct(self, p: float) -> float:
        data = sorted(self.latencies_ms)
        return data[min(int(len(data) * p), len(data) - 1)]

    def summary(self) -> str:
        return (
            f"{sum(self.statuses.values())} req in {self.seconds:.2f}s = {self.rps:.0f} rps, "
            f"p50 {self.pct(0.5):.1f} ms, p99 {self.pct(0.99):.1f} ms, "
            f"statuses {dict(sorted(self.statuses.items()))}"
        )


def blast(server: Server, path: str, client_ip: str, threads: int, per_thread: int) -> LoadResult:
    """``threads`` threads each send ``per_thread`` requests as fast as they can over a
    keep-alive connection; all start together."""
    statuses: Counter = Counter()
    latencies: list[float] = []
    lock = threading.Lock()
    start_gate = threading.Barrier(threads + 1)

    def worker() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        local_statuses: Counter = Counter()
        local_lat = []
        start_gate.wait()
        for _ in range(per_thread):
            t0 = time.perf_counter()
            resp = server.get(path, client_ip, conn)
            local_lat.append((time.perf_counter() - t0) * 1000)
            local_statuses[resp.status] += 1
        conn.close()
        with lock:
            statuses.update(local_statuses)
            latencies.extend(local_lat)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    start_gate.wait()
    t0 = time.perf_counter()
    for t in pool:
        t.join()
    return LoadResult(statuses, latencies, time.perf_counter() - t0)


def manage(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """Run a management command in a *separate process* with the same store settings."""
    code = (
        "import django, sys; from django.core.management import call_command; django.setup(); "
        "call_command(*sys.argv[1:])"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "project.settings",
            "PYTHONPATH": str(ACCEPTANCE_DIR),
            "TW_DEPLOY": "1",
            **env,
        },
        cwd=ACCEPTANCE_DIR,
        capture_output=True,
        text=True,
    )
