"""SQLite persistence. No ORM.

Multi-firm by isolation: every firm has its own database file and upload folder
under DATA_DIR/firms/<id>/. Which firm a connection opens is decided by a
context variable set once per request (from the signed-in session) or per
background job - business code never passes a firm id, so it cannot read
another firm's data by forgetting a WHERE clause. The platform database
(firms, memberships, sessions) lives in platform.py.
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# On Vercel only /tmp is writable, and it is wiped whenever an instance is recycled (see ephemeral()).
DATA_DIR = Path(os.environ.get("CABOT_DATA") or ("/tmp/cabot" if os.environ.get("VERCEL") else
                                                 Path(__file__).resolve().parent.parent / "data"))


def ephemeral() -> bool:
    """True when storage does not survive restarts (Vercel functions): the app shows a demo-only warning."""
    return os.environ.get("CABOT_EPHEMERAL", "1" if os.environ.get("VERCEL") else "0") == "1"

_firm: contextvars.ContextVar[int | None] = contextvars.ContextVar("firm", default=None)


class NoFirm(RuntimeError):
    pass


def current_firm() -> int:
    fid = _firm.get()
    if fid is None:
        raise NoFirm("No firm selected for this operation")
    return fid


def firm_dir(fid: int | None = None) -> Path:
    return DATA_DIR / "firms" / str(current_firm() if fid is None else fid)


def db_path() -> Path:
    return firm_dir() / "cabot.db"


def upload_dir() -> Path:
    return firm_dir() / "uploads"


@contextmanager
def use_firm(fid: int):
    """Run a block against one firm's database (requests set this via auth.current)."""
    token = _firm.set(fid)
    try:
        yield
    finally:
        _firm.reset(token)


def set_firm(fid: int | None) -> None:
    _firm.set(fid)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('staff','partner'))
);
CREATE TABLE IF NOT EXISTS user_clients (
  user_id INT NOT NULL REFERENCES users(id), client_id INT NOT NULL REFERENCES clients(id),
  PRIMARY KEY (user_id, client_id)
);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, pan TEXT UNIQUE, gstin TEXT, tan TEXT,
  entity_type TEXT NOT NULL, gst_state TEXT, gst_scheme TEXT NOT NULL DEFAULT 'none',
  turnover_cr REAL DEFAULT 0, tax_audit INT DEFAULT 0, tds INT DEFAULT 0, tcs INT DEFAULT 0,
  payroll INT DEFAULT 0, advance_tax INT DEFAULT 0, presumptive INT DEFAULT 0,
  transfer_pricing INT DEFAULT 0, salaried INT DEFAULT 0,
  contact_name TEXT, phone TEXT, email TEXT, assignee_id INT REFERENCES users(id),
  aliases TEXT DEFAULT '[]', created_at TEXT, archived INT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, filename TEXT, stored_path TEXT, size INT,
  text_excerpt TEXT, client_id INT REFERENCES clients(id), doc_type TEXT, fy INT,
  period_start TEXT, period_end TEXT, confidence REAL, confs TEXT DEFAULT '{}', status TEXT NOT NULL,
  reasons TEXT DEFAULT '[]', batch_id TEXT, uploaded_by INT, uploaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_doc_client ON documents(client_id, doc_type);
CREATE TABLE IF NOT EXISTS obligations (
  id INTEGER PRIMARY KEY, client_id INT NOT NULL REFERENCES clients(id), rule_code TEXT NOT NULL,
  fy INT NOT NULL, period_key TEXT NOT NULL, months TEXT NOT NULL, due_default TEXT NOT NULL,
  due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', assignee_id INT, ack_no TEXT,
  filed_on TEXT, notes TEXT, updated_at TEXT,
  UNIQUE (client_id, rule_code, fy, period_key)
);
CREATE INDEX IF NOT EXISTS ix_obl_due ON obligations(due);
CREATE TABLE IF NOT EXISTS due_overrides (
  id INTEGER PRIMARY KEY, rule_code TEXT NOT NULL, fy INT NOT NULL, period_key TEXT NOT NULL,
  new_due TEXT NOT NULL, reason TEXT, status TEXT NOT NULL DEFAULT 'proposed',
  proposed_by INT, approved_by INT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS calendar_signoff (fy INT PRIMARY KEY, signed_by INT, signed_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, client_id INT REFERENCES clients(id), channel TEXT, subject TEXT, body TEXT,
  status TEXT NOT NULL DEFAULT 'draft', created_at TEXT, approved_by INT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY, at TEXT, actor INT, kind TEXT, entity TEXT, entity_id INT, text TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES users(id), created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS consent_log (
  id INTEGER PRIMARY KEY, client_id INT NOT NULL, action TEXT NOT NULL, channel TEXT, purposes TEXT, notes TEXT,
  recorded_by INT, at TEXT
);
CREATE TABLE IF NOT EXISTS dsr (
  id INTEGER PRIMARY KEY, client_id INT, requester TEXT, kind TEXT NOT NULL, details TEXT, channel TEXT,
  received_at TEXT, due_at TEXT, status TEXT NOT NULL DEFAULT 'open', response TEXT, closed_at TEXT, handled_by INT
);
CREATE TABLE IF NOT EXISTS breaches (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, data_affected TEXT, client_ids TEXT DEFAULT '[]',
  detected_at TEXT NOT NULL, board_intimated_at TEXT, board_report_at TEXT, principals_notified_at TEXT,
  measures TEXT, status TEXT NOT NULL DEFAULT 'open', recorded_by INT
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, browser TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS learned (
  id INTEGER PRIMARY KEY, kind TEXT, token TEXT, value TEXT, created_at TEXT, UNIQUE(kind, token)
);
"""

BOOL_FIELDS = ("tax_audit", "tds", "tcs", "payroll", "advance_tax", "presumptive", "transfer_pricing", "salaried")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    upload_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=10)
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


# Columns added after the first release. init() adds whichever are missing, so an
# existing office database upgrades in place on the next start.
MIGRATIONS = {
    "clients": {"consent_status": "TEXT DEFAULT 'pending'", "consent_at": "TEXT", "notice_at": "TEXT",
                "is_minor": "INT DEFAULT 0", "guardian_name": "TEXT", "erased_at": "TEXT"},
    "users": {"google_sub": "TEXT", "email": "TEXT", "pw_hash": "TEXT", "perms": "TEXT DEFAULT '[]'", "active": "INT DEFAULT 1",
              "must_change_pw": "INT DEFAULT 0", "phone": "TEXT", "level": "TEXT",
              "client_access": "TEXT DEFAULT 'all'", "last_login": "TEXT"},
    "documents": {"ocr_status": "TEXT DEFAULT ''", "hint_client_id": "INT", "source": "TEXT DEFAULT 'upload'"},
    "messages": {"direction": "TEXT DEFAULT 'out'", "from_phone": "TEXT", "wa_message_id": "TEXT",
                 "error": "TEXT", "sent_by": "INT"},
}


def init() -> None:
    """Create or upgrade the current firm's database."""
    with tx() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_user_email ON users(email) WHERE email IS NOT NULL")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_user_google ON users(google_sub) WHERE google_sub IS NOT NULL")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_wa ON messages(wa_message_id) WHERE wa_message_id IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS ix_doc_ocr ON documents(ocr_status)")


def rows(conn, sql, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def row(conn, sql, args=()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def client_dict(r: dict) -> dict:
    r = dict(r)
    for f in BOOL_FIELDS:
        r[f] = bool(r.get(f))
    r["aliases"] = json.loads(r.get("aliases") or "[]")
    return r


def get_setting(conn, key, default=None):
    r = row(conn, "SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(conn, key, value) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def log(conn, actor, kind, entity, entity_id, text) -> None:
    conn.execute("INSERT INTO activity(at,actor,kind,entity,entity_id,text) VALUES(?,?,?,?,?,?)",
                 (now(), actor, kind, entity, entity_id, text))
