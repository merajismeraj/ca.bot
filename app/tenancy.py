"""The platform: which firms exist, who belongs to which firm, and sign-in sessions.

Holds no client data. A person is identified by their Google account; a
membership links that identity to a user record inside one firm's own
database. Platform owners (CABOT_PLATFORM_ADMINS) approve and suspend firms but
cannot open any firm's data.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','suspended')),
  owner_email TEXT, created_at TEXT, approved_at TEXT, approved_by TEXT, wa_phone_number_id TEXT UNIQUE,
  terms_accepted_at TEXT, terms_accepted_by TEXT
);
CREATE TABLE IF NOT EXISTS memberships (
  email TEXT NOT NULL, firm_id INT NOT NULL REFERENCES firms(id), user_id INT NOT NULL,
  PRIMARY KEY (email, firm_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, email TEXT NOT NULL, google_sub TEXT, name TEXT, firm_id INT, user_id INT,
  created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, browser TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS platform_log (id INTEGER PRIMARY KEY, at TEXT, actor TEXT, text TEXT);
"""
SESSION_DAYS = 14


def path():
    return db.DATA_DIR / "platform.db"


def connect() -> sqlite3.Connection:
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
    _adopt_single_office()
    for f in firms():
        with db.use_firm(f["id"]):
            db.init()


def admins() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("CABOT_PLATFORM_ADMINS", "").split(",") if e.strip()}


def is_admin(email: str | None) -> bool:
    return bool(email) and email.lower() in admins()


def log(actor: str, text: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO platform_log(at, actor, text) VALUES(?,?,?)", (db.now(), actor, text))


# ------------------------------------------------------------------ firms
def firms(status: str | None = None) -> list[dict]:
    with tx() as c:
        q = "SELECT * FROM firms" + (" WHERE status=?" if status else "") + " ORDER BY id"
        return db.rows(c, q, (status,) if status else ())


def firm(fid: int) -> dict | None:
    with tx() as c:
        return db.row(c, "SELECT * FROM firms WHERE id=?", (fid,))


def _slug(name: str, c) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "firm"
    slug, n = base, 2
    while db.row(c, "SELECT id FROM firms WHERE slug=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_firm(name: str, owner_email: str, owner_name: str, google_sub: str | None, status: str) -> tuple[dict, int]:
    """Create a firm, its database, and its super admin. Returns (firm, owner's user id)."""
    from . import auth
    owner_email = owner_email.lower()
    with tx() as c:
        fid = c.execute("INSERT INTO firms(name, slug, status, owner_email, created_at, terms_accepted_at, terms_accepted_by) "
                        "VALUES(?,?,?,?,?,?,?)", (name.strip(), _slug(name, c), status, owner_email, db.now(), db.now(),
                                                  owner_email)).lastrowid
    with db.use_firm(fid):
        db.init()
        with db.tx() as fc:
            db.set_setting(fc, "firm_name", name.strip())
            db.set_setting(fc, "tracking_start", _today().isoformat())
            uid = fc.execute("INSERT INTO users(name, role, email, google_sub, level, perms, client_access, active) "
                             "VALUES(?,?,?,?,?,?,?,1)", (owner_name or owner_email, "partner", owner_email, google_sub,
                                                         "owner", None, "all")).lastrowid
            db.log(fc, uid, "setup", "office", None, f"{owner_name or owner_email} created the firm workspace and accepted the Terms of Use")
            auth.ensure_levels(fc)
    with tx() as c:
        c.execute("INSERT INTO memberships(email, firm_id, user_id) VALUES(?,?,?)", (owner_email, fid, uid))
    log(owner_email, f"created firm #{fid} {name.strip()} ({status})")
    return firm(fid), uid


def set_status(fid: int, status: str, actor: str) -> dict:
    with tx() as c:
        c.execute("UPDATE firms SET status=?, approved_at=COALESCE(approved_at, CASE WHEN ?='active' THEN ? END), "
                  "approved_by=COALESCE(approved_by, CASE WHEN ?='active' THEN ? END) WHERE id=?",
                  (status, status, db.now(), status, actor, fid))
        if status == "suspended":
            c.execute("DELETE FROM sessions WHERE firm_id=?", (fid,))
    log(actor, f"set firm #{fid} to {status}")
    return firm(fid)


def _today():
    from . import engine
    return engine.today()


# ------------------------------------------------------------ memberships
def memberships(email: str) -> list[dict]:
    with tx() as c:
        return db.rows(c, "SELECT m.*, f.name firm_name, f.slug, f.status FROM memberships m JOIN firms f ON f.id=m.firm_id "
                          "WHERE m.email=? ORDER BY f.name", (email.lower(),))


def add_membership(email: str, fid: int, uid: int) -> None:
    with tx() as c:
        if db.row(c, "SELECT 1 FROM memberships WHERE email=? AND firm_id=?", (email.lower(), fid)):
            raise ValueError("This person is already on the team")
        c.execute("INSERT INTO memberships(email, firm_id, user_id) VALUES(?,?,?)", (email.lower(), fid, uid))


def change_membership_email(old: str, new: str, fid: int) -> None:
    with tx() as c:
        c.execute("UPDATE memberships SET email=? WHERE email=? AND firm_id=?", (new.lower(), old.lower(), fid))
        c.execute("DELETE FROM sessions WHERE email=? AND firm_id=?", (old.lower(), fid))


def end_user_sessions(fid: int, uid: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE firm_id=? AND user_id=?", (fid, uid))


# --------------------------------------------------------------- sessions
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(email: str, name: str | None, google_sub: str | None, fid: int | None = None, uid: int | None = None) -> str:
    """A signed-in Google identity, optionally already inside a firm."""
    token = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE expires_at<?", (db.now(),))
        c.execute("INSERT INTO sessions(token,email,google_sub,name,firm_id,user_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                  (_digest(token), email.lower(), google_sub, name, fid, uid, db.now(), exp))
    return token


def session(token: str | None) -> dict | None:
    if not token:
        return None
    with tx() as c:
        return db.row(c, "SELECT * FROM sessions WHERE token=? AND expires_at>?", (_digest(token), db.now()))


def enter_firm(token: str, fid: int, uid: int) -> None:
    with tx() as c:
        c.execute("UPDATE sessions SET firm_id=?, user_id=? WHERE token=?", (fid, uid, _digest(token)))


def end_session(token: str | None) -> None:
    if token:
        with tx() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (_digest(token),))


# ------------------------------------------------------- legacy upgrade
def _adopt_single_office() -> None:
    """Before multi-firm, one office lived in DATA_DIR/cabot.db. Move it into firm #1, unchanged."""
    legacy = db.DATA_DIR / "cabot.db"
    with tx() as c:
        if not legacy.exists() or db.row(c, "SELECT id FROM firms LIMIT 1"):
            return
    dest = db.firm_dir(1)
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = db.DATA_DIR / f"cabot.db{suffix}"
        if src.exists():
            shutil.move(str(src), str(dest / f"cabot.db{suffix}"))
    if (db.DATA_DIR / "uploads").exists():
        shutil.move(str(db.DATA_DIR / "uploads"), str(dest / "uploads"))
    with db.use_firm(1):
        db.init()
        with db.tx() as fc:
            old_root = str(db.DATA_DIR / "uploads")
            fc.execute("UPDATE documents SET stored_path=REPLACE(stored_path, ?, ?) WHERE stored_path LIKE ?",
                       (old_root, str(dest / "uploads"), old_root + "%"))
            name = db.get_setting(fc, "firm_name", "My CA Office")
            people = db.rows(fc, "SELECT * FROM users WHERE email IS NOT NULL")
            owner = next((u["email"] for u in people if u["role"] == "partner"), None)
    with tx() as c:
        c.execute("INSERT INTO firms(id, name, slug, status, owner_email, created_at, approved_at, approved_by) "
                  "VALUES(1,?,?,?,?,?,?,?)", (name, _slug(name, c), "active", owner, db.now(), db.now(), "upgrade"))
        for u in people:
            c.execute("INSERT OR IGNORE INTO memberships(email, firm_id, user_id) VALUES(?,?,?)", (u["email"].lower(), 1, u["id"]))
