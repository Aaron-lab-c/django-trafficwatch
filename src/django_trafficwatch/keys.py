"""Built-in client-identification functions. Any ``(request) -> str`` works as ``KEY_FUNC``.

Security note: ``X-Forwarded-For`` is client-controlled. It is only honoured when the
direct peer (``REMOTE_ADDR``) is listed in ``TRAFFICWATCH["TRUSTED_PROXIES"]``; the
rightmost address not belonging to a trusted proxy is then taken as the client. With the
default (no trusted proxies) the header is ignored entirely, so a client cannot dodge a
limit by forging it.

IPv6: a single subscriber usually owns a whole /64 and may rotate addresses inside it
(privacy extensions), so ``client_ip`` collapses IPv6 addresses to ``IPV6_PREFIX`` bits
(default 64) and returns the network, e.g. ``2001:db8:1:2::/64``. IPv4 is untouched.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from functools import lru_cache
from typing import Any, Union

from .conf import tw_settings

_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
_Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Memcached forbids whitespace/control chars and keys longer than 250 bytes.
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")
MAX_KEY_PART = 120


@lru_cache(maxsize=32)
def _parse_networks(raw: tuple[str, ...]) -> tuple[_Network, ...]:
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in raw)


def _parse_address(addr: str) -> _Address | None:
    try:
        return ipaddress.ip_address(addr)
    except ValueError:
        return None


def _is_trusted(addr: str, trusted: tuple[_Network, ...]) -> bool:
    ip = _parse_address(addr)
    return ip is not None and any(ip in net for net in trusted)


def resolve_client_ip(request: Any) -> str:
    """The raw client address: ``REMOTE_ADDR``, or the rightmost untrusted ``X-Forwarded-For``
    hop when the peer is a trusted proxy. No prefix normalisation (see ``client_ip``)."""
    remote: str = request.META.get("REMOTE_ADDR", "") or "unknown"
    trusted = _parse_networks(tuple(tw_settings.TRUSTED_PROXIES))
    if not trusted or not _is_trusted(remote, trusted):
        return remote

    xff: str = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    # Walk from the proxy nearest to us outward; the first untrusted hop is the client.
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    # Every hop was a trusted proxy (or header missing): the peer itself is the client.
    return hops[0] if hops else remote


def normalise_ip(addr: str, ipv6_prefix: int) -> str:
    """Collapse an IPv6 address to its ``ipv6_prefix`` network (``2001:db8::/64``).
    IPv4 (including IPv4-mapped IPv6) and unparsable values are returned unchanged."""
    ip = _parse_address(addr)
    if ip is None or ip.version != 6:
        return addr
    assert isinstance(ip, ipaddress.IPv6Address)
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return str(mapped)
    if ipv6_prefix >= 128:
        return addr
    return str(ipaddress.ip_network((ip, ipv6_prefix), strict=False))


def client_ip(request: Any) -> str:
    """Client identity for keying: trusted-proxy aware and IPv6-prefix normalised."""
    return normalise_ip(resolve_client_ip(request), int(tw_settings.IPV6_PREFIX))


def is_exempt_client(request: Any) -> bool:
    """True when the resolved client IP is in ``EXEMPT_CLIENTS``."""
    exempt = _parse_networks(tuple(tw_settings.EXEMPT_CLIENTS))
    return bool(exempt) and _is_trusted(resolve_client_ip(request), exempt)


def user_or_ip(request: Any) -> str:
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
