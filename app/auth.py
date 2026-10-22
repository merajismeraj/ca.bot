"""Who is signed in, which firm they are in, and what they may do there.

Sign-in is Google only. A session belongs to a Google identity; once the person
picks (or creates) a firm, the session carries that firm and the matching user
record in the firm's own database. Every request then runs against that firm's
database only (see db.use_firm).

Inside a firm, access has two independent dimensions, both set by the firm's
super admin (or anyone with the Team permission):
  level         a named bundle of permissions (Owner, Partner, Manager, Staff, Article, Viewer);
                the firm can tune each level, and override permissions per person
  client access "all" clients, or only "selected" client accounts (plus anything assigned to them)
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from . import db, tenancy

COOKIE = "cab_session"

PERMS = {
    "view_documents": "Open and download client documents",
    "work": "Upload files, sort the Inbox and move filings through the workflow",
    "approve": "Approve filings, due-date extensions, N/A and reopening",
    "manage_clients": "Add and edit client profiles",
    "send_messages": "Send messages to clients",
    "export": "Download client files as a zip",
    "privacy": "Handle data-protection requests, breaches and erasure",
    "manage_team": "Add team members, set levels and client access, change firm settings",
}
LEVELS = {  # level: (label, default permissions, default client access)
    "owner": ("Super admin", list(PERMS), "all"),
    "partner": ("Partner", ["view_documents", "work", "approve", "manage_clients", "send_messages", "export", "privacy"], "all"),
    "manager": ("Manager", ["view_documents", "work", "manage_clients", "send_messages", "export"], "all"),
    "staff": ("Staff", ["view_documents", "work", "send_messages"], "selected"),
    "article": ("Article / trainee", ["view_documents", "work"], "selected"),
    "viewer": ("Viewer (read-only)", [], "selected"),
}
LEVEL_ORDER = list(LEVELS)


# ----------------------------------------------------------------- levels
def level_perms(conn) -> dict[str, list[str]]:
    """The firm's permission set per level (owner is fixed: everything)."""
    saved = db.get_setting(conn, "levels", {}) or {}
    out = {k: [p for p in saved.get(k, v[1]) if p in PERMS] for k, v in LEVELS.items()}
    out["owner"] = list(PERMS)
    return out


def ensure_levels(conn) -> None:
    """Give users from older versions a level (single-office installs had partner/staff + a permission list)."""
    legacy = db.rows(conn, "SELECT * FROM users WHERE level IS NULL ORDER BY id")
    has_owner = bool(db.row(conn, "SELECT id FROM users WHERE level='owner'"))
    renames = {"manage_clients": "manage_clients", "send_messages": "send_messages", "export": "export",
               "approve": "approve", "manage_team": "manage_team"}
    for u in legacy:
        if u["role"] == "partner":
            level, perms, access = ("partner", None, "all") if has_owner else ("owner", None, "all")
            has_owner = True
        else:
            old = set(json.loads(u["perms"] or "[]"))
            perms = json.dumps(sorted({"view_documents", "work"} | {renames[p] for p in old if p in renames}))
            level, access = "staff", ("all" if "all_clients" in old else "selected")
        conn.execute("UPDATE users SET level=?, perms=?, client_access=? WHERE id=?", (level, perms, access, u["id"]))


def perms_of(user: dict, conn=None) -> set[str]:
    if user.get("level") == "owner":
        return set(PERMS)
    if user.get("perms"):  # a per-person override
        try:
            return set(json.loads(user["perms"])) & set(PERMS)
        except ValueError:
            return set()
    if conn is None:
        with db.tx() as c:
            return set(level_perms(c).get(user.get("level") or "viewer", []))
    return set(level_perms(conn).get(user.get("level") or "viewer", []))


def can(user: dict, perm: str) -> bool:
    if "_perms" not in user:
        user["_perms"] = perms_of(user)
    return perm in user["_perms"]


def require(user: dict, perm: str) -> None:
    if not can(user, perm):
        raise HTTPException(403, f"You don't have permission to {PERMS[perm][0].lower() + PERMS[perm][1:]}")


def public(user: dict) -> dict:
    out = {k: user.get(k) for k in ("id", "name", "role", "email", "active", "phone", "level", "client_access")}
    out["perms"] = sorted(perms_of(user))
    out["custom_perms"] = bool(user.get("perms")) and user.get("level") != "owner"
    out["level_label"] = LEVELS.get(user.get("level") or "viewer", ("?",))[0]
    out["google_linked"] = bool(user.get("google_sub"))
    return out


def role_for(level: str) -> str:
    """Coarse role kept in the users table for older code paths."""
    return "partner" if level in ("owner", "partner") else "staff"


# ---------------------------------------------------------- client access
def visible_client_ids(conn, user: dict) -> set[int] | None:
    """None = every client. Otherwise the selected client accounts plus anything assigned to the person."""
    if user.get("level") == "owner" or (user.get("client_access") or "all") == "all":
        return None
    ids = {r["client_id"] for r in db.rows(conn, "SELECT client_id FROM user_clients WHERE user_id=?", (user["id"],))}
    ids |= {r["id"] for r in db.rows(conn, "SELECT id FROM clients WHERE assignee_id=?", (user["id"],))}
    ids |= {r["client_id"] for r in db.rows(conn, "SELECT DISTINCT client_id FROM obligations WHERE assignee_id=?",
                                            (user["id"],))}
    return ids


def require_client(conn, user: dict, client_id: int | None) -> None:
    scope = visible_client_ids(conn, user)
    if scope is not None and client_id is not None and client_id not in scope:
        raise HTTPException(404, "Client not found")


# -------------------------------------------------------------- sessions
def token_from(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.cookies.get(COOKIE)


async def identity(request: Request) -> dict:
    """FastAPI dependency: the signed-in Google identity (may not have chosen a firm yet)."""
    s = tenancy.session(token_from(request))
    if not s:
        raise HTTPException(401, "Please sign in")
    s["is_platform_admin"] = tenancy.is_admin(s["email"])
    s["_token"] = token_from(request)
    return s


async def current(request: Request) -> dict:
    """FastAPI dependency: the signed-in team member, inside their firm.

    Async on purpose: it sets the firm context in the request's own task, so the
    (threadpool) endpoint that follows inherits it and talks only to that firm's database.
    """
    s = await identity(request)
    if not s["firm_id"]:
        raise HTTPException(409, "Choose a firm first")
    f = tenancy.firm(s["firm_id"])
    if not f or f["status"] != "active":
        raise HTTPException(403, "This firm is awaiting approval" if f and f["status"] == "pending"
                            else "This firm's access has been suspended")
    db.set_firm(f["id"])
    with db.tx() as c:
        u = db.row(c, "SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],))
    if not u or (u["email"] or "").lower() != s["email"]:
        raise HTTPException(401, "Your access to this firm has ended")
    u["_firm"] = f
    u["_session"] = s
    return u


# ------------------------------------------------------- brute-force guard
_FAILS: dict[str, deque] = defaultdict(deque)
WINDOW, MAX_FAILS = 15 * 60, 10


def check_rate(key: str) -> None:
    q = _FAILS[key]
    while q and q[0] < time.time() - WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS:
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")


def record_fail(key: str) -> None:
    _FAILS[key].append(time.time())


def secure_cookies() -> bool:
    return os.environ.get("CABOT_SECURE_COOKIES", "0") == "1"


def dev_login_enabled() -> bool:
    """Local development / demos without Google. Never on in production (see main.dev_login)."""
    return os.environ.get("CABOT_DEV_LOGIN", "0") == "1"
