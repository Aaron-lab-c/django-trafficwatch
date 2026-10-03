"""Multi-process correctness: several gunicorn / uvicorn workers must share one exact set of
counters. Needs a shared store, so these run only when ``TW_DEPLOY=1`` and ``TW_REDIS_URL``
(and optionally ``TW_MEMCACHED``) are set::

    TW_DEPLOY=1 TW_REDIS_URL=redis://localhost:6379/4 TW_MEMCACHED=localhost:11211 \\
        pytest -c acceptance/pytest.ini acceptance/deploy -q
"""

import json
import os
import shutil
import uuid

import pytest

from deploy.harness import Server, blast, manage

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TW_DEPLOY") and os.environ.get("TW_REDIS_URL")),
    reason="set TW_DEPLOY=1 and TW_REDIS_URL to run the multi-worker tests",
)

LIMIT = 50
THREADS, PER_THREAD = 16, 25  # 400 concurrent requests against a budget of 50


def stores():
    out = [("redis", {"TW_REDIS_URL": os.environ.get("TW_REDIS_URL", "")})]
    if os.environ.get("TW_MEMCACHED"):
        out.append(("memcached", {"TW_MEMCACHED": os.environ["TW_MEMCACHED"], "TW_REDIS_URL": ""}))
    return out


def params():
    for store, env in stores():
        for backend in ("fixed", "sliding", "redis"):
            yield pytest.param(store, backend, env, id=f"{store}-{backend}")


def fresh_client() -> str:
    """A client identity nobody has used before (keyed through X-Forwarded-For)."""
    n = uuid.uuid4().int
    return f"198.51.{(n >> 8) % 256}.{n % 256}"


@pytest.mark.parametrize("store, backend, env", list(params()))
def test_exact_admission_across_gunicorn_workers(store, backend, env):
    """400 requests from 16 threads against 4 workers: exactly LIMIT are served."""
    client = fresh_client()
    with Server("gunicorn", workers=4, env={**env, "TW_BACKEND": backend}) as srv:
        result = blast(srv, "/bench/", client, THREADS, PER_THREAD)
        assert result.statuses == {200: LIMIT, 429: THREADS * PER_THREAD - LIMIT}, result.summary()
        # Steady state afterwards: still blocked, with an honest Retry-After.
        resp = srv.get("/bench/", client)
        assert resp.status == 429 and int(resp.getheader("Retry-After")) >= 1
        # Another client is unaffected.
        assert srv.get("/bench/", fresh_client()).status == 200

    # Exactly one first-crossing was recorded, and a separate process can read it.
    out = manage({**env, "TW_BACKEND": backend}, "trafficwatch_recent", "--json", "-n", "1000")
    assert out.returncode == 0, out.stderr
    rows = [r for r in json.loads(out.stdout) if r["client"] == f"ip:{client}"]
    assert len(rows) == 1 and rows[0]["rule"] == "bench" and rows[0]["count"] == LIMIT + 1


@pytest.mark.skipif(
    shutil.which("uvicorn") is None and not os.environ.get("VIRTUAL_ENV"), reason="uvicorn"
)
@pytest.mark.parametrize("backend", ["fixed", "redis"])
def test_exact_admission_across_uvicorn_workers(backend):
    pytest.importorskip("uvicorn")
    client = fresh_client()
    env = {"TW_REDIS_URL": os.environ["TW_REDIS_URL"], "TW_BACKEND": backend}
    with Server("uvicorn", workers=4, env=env) as srv:
        result = blast(srv, "/bench/", client, THREADS, PER_THREAD)
        assert result.statuses == {200: LIMIT, 429: THREADS * PER_THREAD - LIMIT}, result.summary()
        # Unrouted requests are counted under ASGI too (global rule, its own counter).
        resp = srv.get("/no-such-url/", fresh_client())
        assert resp.status == 404 and resp.getheader("X-RateLimit-Limit") == "3"


def test_check_fail_level_warning_catches_locmem_and_passes_on_redis():
    """The deploy recommendation: ``manage.py check --fail-level WARNING``."""
    locmem = manage(
        {"TW_REDIS_URL": "", "TW_MEMCACHED": "", "TW_DEBUG": "0"},
        "check",
        "--fail-level",
        "WARNING",
    )
    assert locmem.returncode != 0 and "trafficwatch.W002" in locmem.stderr
    redis = manage(
        {"TW_REDIS_URL": os.environ["TW_REDIS_URL"], "TW_MEMCACHED": "", "TW_DEBUG": "0"},
        "check",
        "--fail-level",
        "WARNING",
    )
    assert redis.returncode == 0, redis.stderr + redis.stdout
