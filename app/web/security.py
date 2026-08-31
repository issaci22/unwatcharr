"""Authentication, session handling, and request-origin checking.

scrypt from the standard library rather than passlib/bcrypt: one fewer
dependency and no C build step, which matters when the whole point is a small
image.

The threat model is a household LAN, not the open internet — the README is
explicit that this port should not be exposed. Within that model the job is to
stop a curious housemate and a malicious web page, not a determined attacker
with the database in hand.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import HTTPException, Request

from .. import store

log = logging.getLogger(__name__)

SESSION_KEY = "authed"
SESSION_STAMP = "pw"

# Deliberately modest work factor: a NAS may be a low-power box, and a login
# that takes two seconds trains people to leave auth off entirely.
_N, _R, _P = 2**14, 8, 1

# Login rate limiting, per client address, in memory. Process-local like
# everything else here -- see the one-worker rule.
MAX_ATTEMPTS = 10
WINDOW_SECONDS = 900
_attempts: dict[str, Deque[float]] = defaultdict(deque)

# Methods that change something and therefore need an origin check.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"), n=_N, r=_R, p=_P
    )
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    if not stored_hash or not salt:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"), n=_N, r=_R, p=_P
    )
    return hmac.compare_digest(digest.hex(), stored_hash)


def password_is_set() -> bool:
    return bool(store.get_config("ui_password_hash"))


def _password_stamp() -> str:
    """A short fingerprint of the current password.

    Stored in the session so that changing or clearing the password invalidates
    every existing session rather than leaving old cookies working.
    """
    digest = str(store.get_config("ui_password_hash") or "")
    return digest[:16]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limited(request: Request) -> bool:
    key = _client_key(request)
    cutoff = time.time() - WINDOW_SECONDS
    bucket = _attempts[key]
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= MAX_ATTEMPTS


def record_failure(request: Request) -> None:
    _attempts[_client_key(request)].append(time.time())


def clear_failures(request: Request) -> None:
    _attempts.pop(_client_key(request), None)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def login(request: Request) -> None:
    request.session[SESSION_KEY] = True
    request.session[SESSION_STAMP] = _password_stamp()


def logout(request: Request) -> None:
    request.session.clear()


def is_authed(request: Request) -> bool:
    if not password_is_set():
        return True
    if not request.session.get(SESSION_KEY):
        return False
    # A password change or removal invalidates sessions issued before it.
    return request.session.get(SESSION_STAMP) == _password_stamp()


def require(request: Request) -> None:
    """Gate for /api/ routes. Raises 401 rather than redirecting."""
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Please sign in again.")


# ---------------------------------------------------------------------------
# Origin checking (CSRF)
# ---------------------------------------------------------------------------

def origin_allowed(request: Request) -> bool:
    """Reject cross-site state-changing requests.

    The session cookie is SameSite=Lax, which already blocks cross-site POSTs
    from carrying it. This is the belt to that pair of braces, and it also
    covers a same-site-but-different-origin page.

    `Sec-Fetch-Site` is the modern signal and is sent by every current browser.
    `Origin` is the fallback. A request with neither is a non-browser client
    (curl, a script, the test suite) and is allowed through — the session cookie
    is what actually authorises it.
    """
    if request.method not in UNSAFE_METHODS:
        return True

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site in ("same-origin", "same-site", "none")

    origin = request.headers.get("origin")
    if not origin:
        return True

    host = request.headers.get("host", "")
    if not host:
        return False
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    return f"{parsed.hostname}:{parsed.port}" == host or parsed.hostname == host.split(":")[0]


def guard_origin(request: Request) -> None:
    if not origin_allowed(request):
        log.warning(
            "Blocked a cross-origin %s to %s from %s",
            request.method,
            request.url.path,
            request.headers.get("origin"),
        )
        raise HTTPException(
            status_code=403, detail="Cross-site requests are not allowed."
        )
