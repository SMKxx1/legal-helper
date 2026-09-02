"""Optional IP allowlist for the admin plane (PLAN §6 — "optional IP allowlist on /admin").

A FastAPI dependency wave-B admin pages can bolt onto their existing ``require_admin`` guard. It is
**allow-by-default**: with no allowlist configured every client passes, so wiring it early costs
nothing and turns real once an allowlist is provided.

Config gap (config.py is frozen this wave): there is no ``admin_ip_allowlist`` settings field yet, so
the allowlist source is read via ``getattr(settings, "admin_ip_allowlist", None)`` — the dependency is
ready the moment such a field is added (list[str] or a comma/space-separated string), and until then it
is a transparent pass-through. See the reported config gap.

Client-IP trust: an allowlist is a security gate, so a client-supplied ``X-Forwarded-For`` is honoured
**only** behind a trusted edge (``settings.trust_forwarded_proto`` — the same trusted-edge signal the
auth cookies use for the Secure flag). Without that signal the direct socket peer is used, so a
directly-reachable API can't be tricked into treating a spoofed ``X-Forwarded-For`` as allowlisted.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from fastapi import Request

from app.api.errors import EngineError
from app.config import settings
from app.telemetry import get_logger

log = get_logger("nda.auth.admin_ip")


def parse_allowlist(raw: object) -> tuple[str, ...]:
    """Normalise a configured allowlist into a tuple of non-empty entries.

    Accepts a list/tuple/set of strings or a single comma/space/semicolon-separated string; anything
    else (or ``None``) yields an empty tuple ("unset" -> allow all).
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts: Iterable[str] = raw.replace(";", ",").replace(" ", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        return ()
    return tuple(p.strip() for p in parts if p and p.strip())


def configured_allowlist(cfg=settings) -> tuple[str, ...]:
    """The effective admin allowlist entries (empty => allow all). Reads the future
    ``admin_ip_allowlist`` field if/when it exists on ``Settings``."""
    return parse_allowlist(getattr(cfg, "admin_ip_allowlist", None))


def client_ip(request: Request, cfg=settings) -> str:
    """The client IP used for the allowlist check. Honours ``X-Forwarded-For`` (first hop) only when
    a trusted edge is declared (``trust_forwarded_proto``); otherwise the direct socket peer."""
    if getattr(cfg, "trust_forwarded_proto", False):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def ip_allowed(ip: str, allowlist: Iterable[str]) -> bool:
    """True if ``ip`` matches any allowlist entry (exact address or CIDR network). A malformed entry
    or a malformed client IP never matches and never raises (fail-closed on that entry)."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue  # skip a malformed allowlist entry rather than crash the gate
    return False


def require_admin_ip(request: Request) -> None:
    """Dependency: pass when the admin allowlist is unset, or when the client IP is on it; 403
    otherwise. Compose after ``require_admin`` on a wave-B admin route:
    ``dependencies=[Depends(require_admin), Depends(require_admin_ip)]``."""
    allowlist = configured_allowlist(settings)
    if not allowlist:
        return  # allow-by-default: no allowlist configured
    ip = client_ip(request, settings)
    if ip_allowed(ip, allowlist):
        return
    log.warning("auth.admin_ip.denied", ip=ip)
    raise EngineError(
        403,
        "admin_ip_forbidden",
        "Your network is not permitted to access the admin console.",
    )
