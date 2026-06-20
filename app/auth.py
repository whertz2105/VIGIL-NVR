"""Self-contained admin authentication for the Settings GUI.

Design: one fixed admin user (`admin`), password-only, no user management. This
is INDEPENDENT of any upstream proxy auth — it stands alone so
the open-source image is safe even with no proxy in front.

Security properties:
  * Passwords are stored only as an argon2 hash in /data/admin.json (never the
    plaintext, never logged — except the one-time first-run generated password).
  * First run with no password set: a strong random password is generated, only
    its hash is stored, and the plaintext is printed ONCE to the container logs.
  * Sessions are signed httpOnly SameSite=Lax cookies (Starlette SessionMiddleware
    with the secret below); they expire (config.SESSION_MAX_AGE).
  * Login is rate-limited per client IP (5 failures / minute → temporary lockout).

The admin file is NEVER exposed by any API route.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

import config

log = logging.getLogger("nvr.auth")

ADMIN_USER = "admin"
RATE_MAX = 5            # failed attempts ...
RATE_WINDOW = 60        # ... per this many seconds, per IP
MIN_PASSWORD_LEN = 8

_ph = PasswordHasher()
_LOCK = threading.RLock()
_PATH = Path(config.CACHE_DIR) / "admin.json"
_data: dict = {}        # {"password_hash": str, "session_secret": str}

_rl_lock = threading.Lock()
_failures: dict[str, deque] = defaultdict(deque)


# --------------------------- persistence ---------------------------

def _load() -> None:
    global _data
    try:
        if _PATH.is_file():
            with open(_PATH) as f:
                d = json.load(f)
            if isinstance(d, dict):
                _data = d
    except (OSError, ValueError):
        log.warning("admin.json unreadable; treating as first run")
        _data = {}


def _save() -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(_data, f)
    os.replace(tmp, _PATH)
    try:
        os.chmod(_PATH, 0o600)
    except OSError:
        pass


def init() -> None:
    """First-run setup: ensure a session secret and admin password hash exist.
    Generates + announces a random password if none is set. Idempotent."""
    with _LOCK:
        _load()
        changed = False
        if not _data.get("session_secret"):
            _data["session_secret"] = secrets.token_hex(32)
            changed = True
        if not _data.get("password_hash"):
            pw = secrets.token_urlsafe(12)
            _data["password_hash"] = _ph.hash(pw)
            changed = True
            _announce_first_run_password(pw)
        if changed:
            _save()


def _announce_first_run_password(pw: str) -> None:
    line = "=" * 64
    msg = (
        f"\n{line}\n"
        f"  NVR Viewer — admin account created (first run)\n"
        f"  Log in at  /login   with:\n"
        f"      username: {ADMIN_USER}\n"
        f"      password: {pw}\n"
        f"  Change it from the Settings page. This is shown ONCE.\n"
        f"{line}\n"
    )
    print(msg, flush=True)
    log.warning("first-run admin password generated (shown once above)")


def get_session_secret() -> str:
    with _LOCK:
        s = _data.get("session_secret")
        if not s:
            s = secrets.token_hex(32)
            _data["session_secret"] = s
            _save()
        return s


# --------------------------- password ops ---------------------------

def verify_password(pw: str) -> bool:
    with _LOCK:
        h = _data.get("password_hash")
    if not h or not pw:
        return False
    try:
        _ph.verify(h, pw)
    except (Argon2Error, ValueError, TypeError):
        return False
    # Transparently upgrade the hash if argon2's defaults have changed.
    try:
        if _ph.check_needs_rehash(h):
            with _LOCK:
                _data["password_hash"] = _ph.hash(pw)
                _save()
    except (Argon2Error, OSError):
        pass
    return True


def set_password(current: str, new: str) -> None:
    """Change the admin password. Raises PermissionError if `current` is wrong,
    ValueError if `new` is too weak."""
    if not verify_password(current):
        raise PermissionError("current password is incorrect")
    if not isinstance(new, str) or len(new) < MIN_PASSWORD_LEN:
        raise ValueError(f"new password must be at least {MIN_PASSWORD_LEN} characters")
    with _LOCK:
        _data["password_hash"] = _ph.hash(new)
        _save()


# --------------------------- rate limiting ---------------------------

def rate_allowed(ip: str) -> bool:
    """False if this IP has hit the failed-attempt cap within the window."""
    now = time.time()
    with _rl_lock:
        dq = _failures[ip]
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        return len(dq) < RATE_MAX


def rate_record_failure(ip: str) -> None:
    with _rl_lock:
        _failures[ip].append(time.time())


def rate_reset(ip: str) -> None:
    with _rl_lock:
        _failures.pop(ip, None)


# --------------------------- session helpers ---------------------------

def is_admin(request) -> bool:
    try:
        return bool(request.session.get("admin"))
    except Exception:
        return False
