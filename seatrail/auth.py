"""Password sign-in with server-side sessions.

Passwords: PBKDF2-SHA256, 310k iterations, per-user salt. Sessions: a random token in an HttpOnly
cookie; only its SHA-256 is stored, so a database leak does not leak live sessions.
Enterprise deployments should put SSO (OIDC/SAML) in front; this is the built-in fallback.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from . import config, db

ITERATIONS = 310_000
COOKIE = "st_session"
MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256((config.SECRET_KEY + token).encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = db.now()
    db.x("INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
         _token_hash(token), user_id, now, now + config.SESSION_DAYS * 86400)
    return token


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.q1("SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND"
                " s.expires_at>?", _token_hash(token), db.now())
    return row


def end_session(token: str | None) -> None:
    if token:
        db.x("DELETE FROM auth_sessions WHERE token_hash=?", _token_hash(token))


# Very small brute-force guard: 8 failures per email or IP per 15 minutes.
_failures: dict[str, list[float]] = defaultdict(list)
WINDOW, MAX_FAILS = 900, 8


def _recent(key: str) -> list[float]:
    cutoff = time.time() - WINDOW
    _failures[key] = [t for t in _failures[key] if t > cutoff]
    return _failures[key]


def throttled(*keys: str) -> bool:
    return any(len(_recent(k)) >= MAX_FAILS for k in keys)


def record_failure(*keys: str) -> None:
    for k in keys:
        _recent(k).append(time.time())


def authenticate(email: str, password: str, ip: str) -> dict | None:
    email = (email or "").strip().lower()
    if throttled("e:" + email, "ip:" + ip):
        raise PermissionError("too many attempts, try again in 15 minutes")
    user = db.q1("SELECT * FROM users WHERE lower(email)=?", email)
    if not user or not verify_password(password or "", user["password_hash"]):
        record_failure("e:" + email, "ip:" + ip)
        return None
    return user


SELF_SIGNUP_ROLES = {"expert", "buyer"}


def signup(name: str, email: str, password: str, role: str, credentials: str = "") -> dict:
    """Self sign-up is for marketplace experts and buyers. Enterprise users are invited by their admin."""
    if role not in SELF_SIGNUP_ROLES:
        raise ValueError("only experts and buyers can sign up here")
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 200:
        raise ValueError("enter a valid email")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if not (name or "").strip():
        raise ValueError("enter your name")
    if db.q1("SELECT id FROM users WHERE lower(email)=?", email):
        raise ValueError("an account with this email already exists")
    uid = db.x("INSERT INTO users (org_id, role, name, email, credentials, password_hash, created_at)"
               " VALUES (0,?,?,?,?,?,?)", role, name.strip()[:120], email, (credentials or "")[:300],
               hash_password(password), db.now())
    return db.q1("SELECT * FROM users WHERE id=?", uid)


def invite(admin: dict, name: str, email: str, role: str) -> tuple[dict, str]:
    """Org admin adds a teammate and gets a one-time temporary password to hand over."""
    if role not in ("employee", "steward", "ml_engineer", "org_admin"):
        raise ValueError("unknown enterprise role")
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("enter a valid email")
    if db.q1("SELECT id FROM users WHERE lower(email)=?", email):
        raise ValueError("an account with this email already exists")
    temp = secrets.token_urlsafe(12)
    uid = db.x("INSERT INTO users (org_id, role, name, email, password_hash, created_at) VALUES (?,?,?,?,?,?)",
               admin["org_id"], role, name.strip()[:120], email, hash_password(temp), db.now())
    db.audit(admin["org_id"], admin["id"], "user.invite", f"user:{uid}", role=role)
    return db.q1("SELECT * FROM users WHERE id=?", uid), temp


def change_password(user: dict, old: str, new: str) -> None:
    if not verify_password(old, user["password_hash"]):
        raise PermissionError("current password is wrong")
    if len(new or "") < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    db.x("UPDATE users SET password_hash=? WHERE id=?", hash_password(new), user["id"])
    db.x("DELETE FROM auth_sessions WHERE user_id=?", user["id"])
