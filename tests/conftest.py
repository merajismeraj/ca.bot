import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CABOT_TODAY"] = "2026-10-22"
os.environ["CABOT_LLM"] = "0"
os.environ["CABOT_WORKER"] = "0"  # tests drive OCR explicitly
for k in ("WA_TOKEN", "WA_PHONE_NUMBER_ID", "WA_VERIFY_TOKEN", "WA_APP_SECRET", "WA_TEMPLATE"):
    os.environ.pop(k, None)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    from app import auth, db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "UPLOAD_DIR", tmp_path / "up")
    auth._FAILS.clear()
    db.init()
    return db


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(fresh):
    """A set-up office with a partner and one staff member; requests carry Bearer tokens (no shared cookie jar)."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={"firm_name": "Test & Co", "name": "Neha", "email": "neha@x.in",
                                            "password": "partner-pass", "accept_terms": True})
        assert r.status_code == 200, r.text
        c.cookies.clear()
        c.partner = bearer(r.json()["token"])
        c.invite = lambda **kw: c.post("/api/users", json={"name": "Arjun", "email": "arjun@x.in"} | kw, headers=c.partner)
        inv = c.invite().json()
        c.staff = bearer(c.post("/api/auth/login", json={"email": "arjun@x.in", "password": inv["temp_password"]}).json()["token"])
        c.staff_id = inv["user"]["id"]
        c.cookies.clear()
        c.headers.update(c.partner)  # default caller; pass headers=c.staff to act as staff
        yield c
