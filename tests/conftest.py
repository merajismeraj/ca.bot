import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CABOT_TODAY"] = "2026-10-22"
os.environ["CABOT_LLM"] = "0"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "UPLOAD_DIR", tmp_path / "up")
    db.init()
    return db


@pytest.fixture
def api(fresh):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        users = c.get("/api/bootstrap").json()["users"]
        c.partner = {"X-User": str(next(u["id"] for u in users if u["role"] == "partner"))}
        c.staff = {"X-User": str(next(u["id"] for u in users if u["role"] == "staff"))}
        yield c
