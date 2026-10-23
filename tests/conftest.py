import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CABOT_TODAY"] = "2026-10-22"
os.environ["CABOT_LLM"] = "0"
os.environ["CABOT_WORKER"] = "0"  # tests drive OCR explicitly
for k in ("WA_TOKEN", "WA_VERIFY_TOKEN", "WA_APP_SECRET", "WA_TEMPLATE", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
          "GOOGLE_ALLOWED_DOMAIN", "CABOT_PLATFORM_ADMINS", "CABOT_OPEN_SIGNUP", "CABOT_DEV_LOGIN"):
    os.environ.pop(k, None)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    from app import auth, db, tenancy
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    auth._FAILS.clear()
    tenancy.init()
    yield db
    db.set_firm(None)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def login(email, name=None):
    """What a successful Google sign-in produces: a session, inside the person's firm if they have exactly one."""
    from app import main, tenancy
    return bearer(main._sign_in(email, name or email.split("@")[0].title(), f"sub-{email}"))


def make_firm(name="Test & Co", owner="neha@x.in"):
    from app import tenancy
    f, _ = tenancy.create_firm(name, owner, owner.split("@")[0].title(), None, "active")
    return f


@pytest.fixture
def api(fresh):
    """A firm with a super admin (default caller) and one staff member (headers=api.staff)."""
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    with TestClient(app) as c:
        c.firm = make_firm()
        db.set_firm(c.firm["id"])  # direct db access from the test itself
        c.partner = login("neha@x.in")
        c.headers.update(c.partner)
        c.invite = lambda **kw: c.post("/api/users", json={"name": "Arjun", "email": "arjun@x.in", "level": "staff",
                                                           "client_access": "all"} | kw, headers=c.partner)
        r = c.invite()
        assert r.status_code == 200, r.text
        c.staff_id = r.json()["id"]
        c.staff = login("arjun@x.in")
        yield c
