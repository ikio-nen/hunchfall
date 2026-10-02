"""Loopback shim fallback for networks that block Polymarket egress.

Some networks cannot open direct TCP connections to gamma-api / clob /
data-api.polymarket.com (every request times out). When the operator runs the
local shim (``backend/scripts/live_shim.py``) and points
``POLYMARKET_SHIM_URL`` at it, a **connection-level** direct failure
(``httpx.ConnectError`` / ``httpx.ConnectTimeout``) may be replayed exactly
once through the shim.

Rules (fail closed):

* Direct is always tried first; the shim is only a fallback.
* Only connection-level failures may fall back — HTTP status errors and
  read timeouts are real upstream answers and never fall back.
* Exactly one replay, no retries.
* The shim is off unless ``POLYMARKET_SHIM_URL`` is set and
  ``POLYMARKET_SHIM_ON_BLOCKED`` is true.
* If the shim itself fails, the raised error names both failures.

Read-only by construction: GET only, no auth, no keys, no order placement —
paper predictions only.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

#: Upstream client -> shim mount point. The live shim serves /gamma, /clob
#: and /data, mapping them to the three public Polymarket APIs.
_MOUNTS = {"gamma": "gamma", "clob": "clob", "data-api": "data"}

#: One shim call may be slower than a direct call by design (it rides an
#: allowed route), but it must not hang a cycle forever.
SHIM_TIMEOUT_SEC = 60.0


class ShimError(RuntimeError):
    """The shim replay failed; the message names both failures."""


def shim_base(settings: Settings) -> str:
    """Shim base URL when the fallback is enabled; "" when disabled.

    Enabled only when ``POLYMARKET_SHIM_URL`` is set AND
    ``POLYMARKET_SHIM_ON_BLOCKED`` is true (the default).

    Args:
        settings: App Settings.

    Returns:
        Trimmed base URL without a trailing slash, or "".
    """
    url = str(getattr(settings, "POLYMARKET_SHIM_URL", "") or "").strip()
    if not url or not bool(getattr(settings, "POLYMARKET_SHIM_ON_BLOCKED", True)):
        return ""
    return url.rstrip("/")


def is_blocked(exc: BaseException) -> bool:
    """True only for connection-level egress failures.

    A blocked route surfaces as ``ConnectError`` (refused/unreachable) or
    ``ConnectTimeout`` (the connect itself times out). A *read* timeout is
    not this: the request reached the server and the server is slow —
    falling back would mask a real upstream incident.

    Args:
        exc: The exception raised by the direct attempt.

    Returns:
        True when the failure is a blocked-connection failure.
    """
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def shim_url(settings: Settings, upstream: str, path: str) -> str | None:
    """Full shim URL for one upstream path; None when disabled/unmapped.

    Args:
        settings: App Settings.
        upstream: "gamma" | "clob" | "data-api".
        path: Path under the upstream base URL, e.g. "/book".

    Returns:
        ``<base>/<mount>/<path>``, or None when the shim is disabled or the
        upstream has no mount.
    """
    base = shim_base(settings)
    mount = _MOUNTS.get(upstream)
    if not base or not mount:
        return None
    return f"{base}/{mount}/{path.lstrip('/')}"


def _client() -> httpx.Client:
    """Client factory for one shim replay (tests inject a MockTransport)."""
    return httpx.Client(timeout=SHIM_TIMEOUT_SEC)


def shim_get(
    settings: Settings,
    upstream: str,
    path: str,
    params: dict | None = None,
    *,
    direct_exc: BaseException,
) -> httpx.Response | None:
    """Replay one blocked GET through the shim.

    Exactly one attempt — no retries, no backoff. The response is returned
    even when its status is >= 400; deciding what a bad status means is the
    caller's job.

    Args:
        settings: App Settings.
        upstream: "gamma" | "clob" | "data-api".
        path: Path under the upstream base URL.
        params: Query params.
        direct_exc: The connection-level error the direct call raised.

    Returns:
        The shim response, or None when the shim is disabled.

    Raises:
        ShimError: the shim itself could not be reached; the message names
            both the direct error and the shim error.
    """
    url = shim_url(settings, upstream, path)
    if url is None:
        return None
    log.warning(
        "%s egress blocked (%s: %s); replaying once via shim %s",
        upstream,
        type(direct_exc).__name__,
        direct_exc,
        url,
    )
    try:
        with _client() as http:
            return http.get(url, params=params)
    except httpx.HTTPError as shim_exc:
        raise ShimError(
            f"{upstream} direct call failed ({type(direct_exc).__name__}: "
            f"{direct_exc}); shim replay failed ({type(shim_exc).__name__}: "
            f"{shim_exc})"
        ) from shim_exc


def shim_fallback(
    settings: Settings,
    upstream: str,
    path: str,
    params: dict | None,
    direct_exc: BaseException,
) -> httpx.Response | None:
    """One shim replay when (and only when) the direct error is blocked.

    Combines :func:`is_blocked` and :func:`shim_get` so every client hook is
    one call: pass the error the direct attempt raised; None means no
    fallback applies and the caller should surface the direct failure as
    before.

    Args:
        settings: App Settings.
        upstream: "gamma" | "clob" | "data-api".
        path: Path under the upstream base URL.
        params: Query params.
        direct_exc: The error the direct call raised.

    Returns:
        The shim response, or None when no fallback applies.

    Raises:
        ShimError: the shim replay itself failed.
    """
    if not is_blocked(direct_exc):
        return None
    return shim_get(settings, upstream, path, params, direct_exc=direct_exc)
