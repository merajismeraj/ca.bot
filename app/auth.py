"""Logins, sessions and per-person permissions.

Partners hold every permission. Staff get a default set that a partner can
widen or narrow per person. Sessions are opaque random tokens stored server
side; the browser carries them in an HttpOnly cookie, API clients as a Bearer
token. Passwords are hashed with scrypt (stdlib, no extra dependency).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from . import db

COOKIE = "cab_session"
SESSION_DAYS = 14

PERMS = {
    "approve": "Approve filings, due-date extensions, N/A and reopening",
    "manage_clients": "Add, edit and archive clients",
    "send_messages": "Send messages to clients",
    "all_clients": "See every client (otherwise only the ones assigned to them)",
    "export": "Download client files as a zip",
    "manage_team": "Invite people, set permissions, change office settings",
}
STAFF_DEFAULT = ["manage_clients", "send_messages", "all_clients", "export"]


# ------------------------------------------------------------- passwords
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, dk = stored.split("$")
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), dk)


def validate_pw(pw: str) -> None:
    if len(pw or "") < 8:
        raise HTTPException(400, "Use at least 8 characters for the password")


def temp_password() -> str:
    # readable over the phone: no 0/O/1/l
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


# ----------------------------------------------------------- permissions
def perms_of(user: dict) -> set[str]:
    if user.get("role") == "partner":
        return set(PERMS)
    try:
        return set(json.loads(user.get("perms") or "[]")) & set(PERMS)
    except ValueError:
        return set()


def can(user: dict, perm: str) -> bool:
    return perm in perms_of(user)


def require(user: dict, perm: str) -> None:
    if not can(user, perm):
        raise HTTPException(403, f"You don't have permission to {PERMS[perm][0].lower() + PERMS[perm][1:]}")


def public(user: dict) -> dict:
    out = {k: user.get(k) for k in ("id", "name", "role", "email", "active", "must_change_pw", "phone")}
    out["perms"] = sorted(perms_of(user))
    out["google_linked"] = bool(user.get("google_sub"))
    return out


def visible_client_ids(conn, user: dict) -> set[int] | None:
    """None = everything. Otherwise the clients this person handles or has filings assigned on."""
    if can(user, "all_clients"):
        return None
    ids = {r["id"] for r in db.rows(conn, "SELECT id FROM clients WHERE assignee_id=?", (user["id"],))}
    ids |= {r["client_id"] for r in db.rows(conn, "SELECT DISTINCT client_id FROM obligations WHERE assignee_id=?",
                                            (user["id"],))}
    return ids


def require_client(conn, user: dict, client_id: int | None) -> None:
    scope = visible_client_ids(conn, user)
    if scope is not None and client_id is not None and client_id not in scope:
        raise HTTPException(404, "Client not found")


# -------------------------------------------------------------- sessions
def new_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                 (_digest(token), user_id, db.now(), exp))
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (db.now(),))
    return token


def _digest(token: str) -> str:
    # only a hash of the token is stored, so a copied database can't be used to log in
    return hashlib.sha256(token.encode()).hexdigest()


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (_digest(token),))


def token_from(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.cookies.get(COOKIE)


def user_for_token(conn, token: str | None) -> dict | None:
    if not token:
        return None
    return db.row(conn, "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE s.token=? AND s.expires_at>? AND u.active=1", (_digest(token), db.now()))


def current(request: Request) -> dict:
    """FastAPI dependency: the signed-in person, or 401."""
    conn = db.connect()
    try:
        u = user_for_token(conn, token_from(request))
    finally:
        conn.close()
    if not u:
        raise HTTPException(401, "Please sign in")
    return u


def setup_needed(conn) -> bool:
    return not db.rows(conn, "SELECT id FROM users WHERE pw_hash IS NOT NULL AND active=1 LIMIT 1")


# ------------------------------------------------------- brute-force guard
_FAILS: dict[str, deque] = defaultdict(deque)
WINDOW, MAX_FAILS = 15 * 60, 5


def check_rate(key: str) -> None:
    q = _FAILS[key]
    while q and q[0] < time.time() - WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes or ask a partner to reset your password.")


def record_fail(key: str) -> None:
    _FAILS[key].append(time.time())


def secure_cookies() -> bool:
    return os.environ.get("CABOT_SECURE_COOKIES", "0") == "1"
