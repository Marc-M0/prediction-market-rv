"""HTTP client factory and the optional in-memory DNS workaround."""
from __future__ import annotations

from functools import lru_cache
import socket

import httpx

from prediction_market_rv.config import HEADERS, USE_CUSTOM_DNS


if USE_CUSTOM_DNS:
    try:
        import dns.resolver

        _resolver = dns.resolver.Resolver()
        _resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        _orig_getaddrinfo = socket.getaddrinfo

        @lru_cache(maxsize=256)
        def _resolve(host: str) -> str | None:
            try:
                return str(_resolver.resolve(host, "A")[0])
            except Exception:
                return None

        def _patched_getaddrinfo(host, port, *args, **kwargs):
            ip = _resolve(host) if isinstance(host, str) else None
            return _orig_getaddrinfo(ip or host, port, *args, **kwargs)

        socket.getaddrinfo = _patched_getaddrinfo
    except ImportError:
        pass


def make_client() -> httpx.Client:
    """One shared client for all API calls (importing this module also installs the DNS workaround)."""
    return httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True)
