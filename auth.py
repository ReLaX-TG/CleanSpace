"""Admin authentication helpers.

Uses only well-tested building blocks:
- werkzeug.security.generate_password_hash / check_password_hash (salted scrypt)
- Flask's signed session cookie (itsdangerous) with a random persistent secret
- secrets.token_hex for generating the secret
No custom cryptography is implemented here.
"""

import hashlib
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db


# ---------------------------------------------------------------------------
# Files in ./instance
# ---------------------------------------------------------------------------
def _write_private_file(path: Path, text: str) -> None:
    """Write atomically (temp file + replace) so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, 0o600)  # owner-only on POSIX; on Windows the user profile ACL applies
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_or_create_secret_key(path: Path) -> str:
    """Random 256-bit secret, created once and reused, so sessions survive restarts."""
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 64:
            return key
    key = secrets.token_hex(32)
    _write_private_file(path, key + "\n")
    return key


def load_admin_password_hash() -> str | None:
    try:
        data = json.loads(config.ADMIN_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    value = data.get("password_hash")
    return value if isinstance(value, str) and value else None


def save_admin_password(password: str) -> None:
    """Store ONLY a salted hash of the password (never the password itself)."""
    password_hash = generate_password_hash(password)  # Werkzeug default: scrypt + random salt
    payload = {
        "password_hash": password_hash,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_private_file(config.ADMIN_CREDENTIALS_FILE, json.dumps(payload, indent=2) + "\n")


def admin_is_configured() -> bool:
    return load_admin_password_hash() is not None


def _credentials_fingerprint(password_hash: str) -> str:
    """Short digest of the stored hash. Changing the password changes it,
    which logs out every existing session."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:32]


def verify_password(password: str) -> bool:
    stored = load_admin_password_hash()
    if stored is None or not isinstance(password, str) or not password:
        return False
    return check_password_hash(stored, password)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def log_in() -> None:
    stored = load_admin_password_hash()
    session.clear()  # drop anything from before login (also rotates the CSRF token)
    session.permanent = True
    session["admin"] = True
    session["cred"] = _credentials_fingerprint(stored)
    session["login_at"] = int(time.time())


def log_out() -> None:
    session.clear()


def is_logged_in() -> bool:
    if session.get("admin") is not True:
        return False
    stored = load_admin_password_hash()
    if stored is None or session.get("cred") != _credentials_fingerprint(stored):
        session.clear()  # password was changed or removed -> old sessions are invalid
        return False
    return True


def admin_page_required(view):
    """For HTML pages: redirect to the login page when not logged in."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)
    return wrapper


def admin_api_required(view):
    """For JSON API: 401 without any request data when not logged in."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return jsonify(error="unauthorized", message="Please log in again."), 401
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Login throttling (no permanent lockout: old failures expire)
# ---------------------------------------------------------------------------
def client_key() -> str:
    return request.remote_addr or "unknown"


def seconds_until_login_allowed(now: float | None = None) -> int:
    """0 if a login attempt is allowed now, otherwise seconds to wait."""
    now = time.time() if now is None else now
    count, oldest = db.recent_login_failures(
        config.DATABASE_PATH, client_key(), now, config.LOGIN_WINDOW_SECONDS
    )
    if count < config.LOGIN_MAX_FAILURES or oldest is None:
        return 0
    return max(1, int(oldest + config.LOGIN_WINDOW_SECONDS - now) + 1)


def register_failed_login() -> None:
    db.record_login_failure(config.DATABASE_PATH, client_key(), time.time(), config.LOGIN_WINDOW_SECONDS)


def reset_failed_logins() -> None:
    db.clear_login_failures(config.DATABASE_PATH, client_key())
