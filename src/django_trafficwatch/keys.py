"""Built-in client-identification functions. Any ``(request) -> str`` works as ``KEY_FUNC``.

Security note: ``X-Forwarded-For`` is client-controlled. It is only honoured when the
direct peer (``REMOTE_ADDR``) is listed in ``TRAFFICWATCH["TRUSTED_PROXIES"]``; the
rightmost address not belonging to a trusted proxy is then taken as the client. With the
default (no trusted proxies) the header is ignored entirely, so a client cannot dodge a
limit by forging it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from functools import lru_cache
from typing import Union

from .conf import tw_settings

_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Memcached forbids whitespace/control chars and keys longer than 250 bytes.
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")
MAX_KEY_PART = 120


@lru_cache(maxsize=32)
def _parse_networks(raw: tuple[str, ...]) -> tuple[_Network, ...]:
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in raw)


def _is_trusted(addr: str, trusted: tuple[_Network, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def client_ip(request) -> str:
    """Best-effort client IP honouring X-Forwarded-For only behind trusted proxies."""
    remote = request.META.get("REMOTE_ADDR", "") or "unknown"
    trusted = _parse_networks(tuple(tw_settings.TRUSTED_PROXIES))
    if not trusted or not _is_trusted(remote, trusted):
        return remote

    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    # Walk from the proxy nearest to us outward; the first untrusted hop is the client.
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    # Every hop was a trusted proxy (or header missing): the peer itself is the client.
    return hops[0] if hops else remote


def user_or_ip(request) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"user:{user.pk}"
    return f"ip:{client_ip(request)}"


def safe_key_part(value: str) -> str:
    """Make an arbitrary identifier safe for every Django cache backend (Memcached rejects
    whitespace, control characters and keys over 250 bytes)."""
    if len(value) <= MAX_KEY_PART and not _UNSAFE.search(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"h:{digest}"
