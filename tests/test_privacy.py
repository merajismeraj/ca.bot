"""DPDP controls: consent, withdrawal enforcement, rights requests, erasure with legal hold, breaches, public pages."""
import base64
import os


def client(api, **kw):
    return api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5", "phone": "9820012345"} | kw).json()


def test_new_firm_requires_terms(api):
    from conftest import login
    h = login("new@firm.in")
    r = api.post("/api/firms", json={"name": "New Firm"}, headers=h)
    assert r.status_code == 400 and "Terms" in r.json()["detail"]


def test_public_pages_without_login(api):
    client(api)
    for path, words in (("/privacy", "Data Fiduciary"), ("/terms", "Limitation of liability"), ("/privacy", "people who sign in")):
        r = api.get(path, headers={"Authorization": ""})
        assert r.status_code == 200 and words in r.text
    api.post("/api/privacy/settings", json={"officer_name": "Neha", "officer_email": "dpo@x.in", "retention_years": 8,
                                            "response_days": 120})
    info = api.get(f"/api/public/info?firm={api.firm['slug']}", headers={"Authorization": ""}).json()
    assert info["scope"] == "firm" and info["firm_name"] == "Test & Co"
    assert api.get("/api/public/info", headers={"Authorization": ""}).json()["scope"] == "platform"
    assert info["officer_email"] == "dpo@x.in" and info["response_days"] == 90  # capped at the Rules' maximum
    assert "ABCPS1234K" not in str(info) and "Sharma" not in str(info)  # no client data on public endpoints


def test_consent_flow_and_minor_guard(api):
    c = client(api)
    assert c["consent_status"] == "pending"
    m = api.post(f"/api/clients/{c['id']}/privacy-notice").json()
    assert m["subject"] == "Privacy notice" and "/privacy" in m["body"] and "Digital Personal Data Protection Act" in m["body"]
    r = api.post(f"/api/clients/{c['id']}/consent", json={"action": "given", "is_minor": True})
    assert r.status_code == 400 and "guardian" in r.json()["detail"]
    r = api.post(f"/api/clients/{c['id']}/consent", json={"action": "given", "is_minor": True, "guardian_name": "Rajesh Sharma",
                                                           "channel": "WhatsApp reply"})
    assert r.json()["consent_status"] == "given"
    log = api.get(f"/api/clients/{c['id']}/privacy").json()["log"]
    assert [l["action"] for l in log] == ["given", "notice_sent"]


def test_withdrawal_stops_processing(api):
    c = client(api)
    api.post("/api/settings", json={"tracking_start": "2026-04-01"})
    r = api.post("/api/privacy/requests", json={"kind": "withdraw", "client_id": c["id"], "channel": "Email"}).json()
    assert r["due_at"] == "2026-11-21" or r["due_at"] > r["received_at"]
    assert api.post(f"/api/clients/{c['id']}/chase").status_code == 400
    up = api.post("/api/upload", data={"client_id": str(c["id"])}, files=[("files", ("s.csv", b"x", "text/csv"))])
    assert up.status_code == 400 and "withdrawn" in up.json()["detail"]
    # a file that would have been auto-filed to this client goes to review instead
    r = api.post("/api/upload", files=[("files", ("sales sep 2026.csv", b"Sales 27ABCPS1234K1Z5", "text/csv"))]).json()
    assert r["files"][0]["status"] == "needs_review" and "withdrawn" in r["files"][0]["reasons"][0]


def test_request_register_and_overdue(api):
    c = client(api)
    r = api.post("/api/privacy/requests", json={"kind": "access", "client_id": c["id"], "received_at": "2026-08-01"}).json()
    ov = api.get("/api/privacy/overview").json()
    req = next(x for x in ov["requests"] if x["id"] == r["id"])
    assert req["overdue"] and not next(x for x in ov["checklist"] if x["key"] == "requests")["ok"]
    assert api.post(f"/api/privacy/requests/{r['id']}/close", json={"response": ""}).status_code == 400
    assert api.post(f"/api/privacy/requests/{r['id']}/close", json={"response": "Summary sent by email"}).json()["status"] == "closed"
    s = api.get(f"/api/clients/{c['id']}/privacy").json()["summary"]
    assert s["profile"]["pan"] == "ABCPS1234K" and s["purposes"]
    assert api.post("/api/privacy/requests", json={"kind": "bogus", "requester": "x"}).status_code == 400


def test_erasure_respects_legal_retention(api):
    from app import db
    c = client(api)
    api.post("/api/upload", files=[("files", ("Sharma Traders sales sep 2026.csv", b"Sales 27ABCPS1234K1Z5 a", "text/csv")),
                                   ("files", ("Sharma Traders sales apr 2012.csv", b"Sales 27ABCPS1234K1Z5 b", "text/csv"))])
    assert api.post(f"/api/clients/{c['id']}/erase", json={"confirm_name": "wrong"}).status_code == 400
    assert api.post(f"/api/clients/{c['id']}/erase", json={"confirm_name": "sharma traders"}, headers=api.staff).status_code == 403
    r = api.post(f"/api/clients/{c['id']}/erase", json={"confirm_name": "Sharma Traders"}).json()
    assert r["erased_files"] == 1 and r["held_files"] == 1 and r["held_until"] == "2035-03-31"
    with db.tx() as conn:
        cl = db.row(conn, "SELECT * FROM clients WHERE id=?", (c["id"],))
        old = db.row(conn, "SELECT * FROM documents WHERE fy=2012")
    assert cl["phone"] is None and cl["archived"] == 1 and cl["pan"] == "ABCPS1234K"  # identity kept while records are held
    assert old["stored_path"] is None and old["status"] == "erased"


def test_full_erasure_when_nothing_held(api):
    c = client(api, name="Old Client", gstin=None, pan="AAAPO1111O")
    r = api.post(f"/api/clients/{c['id']}/erase", json={"confirm_name": "Old Client"}).json()
    assert r["held_files"] == 0
    from app import db
    with db.tx() as conn:
        cl = db.row(conn, "SELECT * FROM clients WHERE id=?", (c["id"],))
    assert cl["name"].startswith("Erased client") and cl["pan"] is None and cl["erased_at"]


def test_retention_purge_never_touches_held_files(api):
    c = client(api)
    r = api.post("/api/upload", files=[("files", ("Sharma Traders sales apr 2012.csv", b"Sales 27ABCPS1234K1Z5 old", "text/csv")),
                                       ("files", ("Sharma Traders sales sep 2026.csv", b"Sales 27ABCPS1234K1Z5 new", "text/csv"))]).json()
    ids = [f["id"] for f in r["files"]]
    due = api.get("/api/privacy/overview").json()["retention_due"]
    assert len(due) == 1
    assert api.post("/api/privacy/retention/purge", json={"ids": ids}).json()["erased"] == 1


def test_breach_register_and_notices(api):
    c = client(api)
    assert api.post("/api/privacy/breaches", json={"title": "x"}, headers=api.staff).status_code == 403
    b = api.post("/api/privacy/breaches", json={"title": "Laptop stolen", "data_affected": "bank statements",
                                                "client_ids": [c["id"]]}).json()
    assert 71 < b["hours_left"] <= 72
    assert not next(x for x in api.get("/api/privacy/overview").json()["checklist"] if x["key"] == "breach")["ok"]
    assert api.post(f"/api/privacy/breaches/{b['id']}/notify").json()["drafted"] == 1
    # breach notices may be sent even to a client who withdrew consent
    api.post(f"/api/clients/{c['id']}/consent", json={"action": "withdrawn"})
    msg = next(m for m in api.get("/api/messages").json() if m["subject"] == "Personal data breach notice")
    assert api.post(f"/api/messages/{msg['id']}", json={"action": "sent"}).status_code == 200
    api.post(f"/api/privacy/breaches/{b['id']}", json={"board_report_at": "2026-10-22T12:00"})
    assert next(x for x in api.get("/api/privacy/overview").json()["checklist"] if x["key"] == "breach")["ok"]


def test_files_encrypted_at_rest(api, monkeypatch):
    from app import db, storage
    monkeypatch.setenv("CABOT_DATA_KEY", base64.b64encode(os.urandom(32)).decode())
    client(api)
    r = api.post("/api/upload", files=[("files", ("Sharma Traders sales sep 2026.csv", b"Sales 27ABCPS1234K1Z5 secret", "text/csv"))]).json()
    f = r["files"][0]
    assert f["status"] == "auto"  # classification still works on encrypted storage
    with db.tx() as conn:
        path = db.row(conn, "SELECT stored_path FROM documents WHERE id=?", (f["id"],))["stored_path"]
    raw = open(path, "rb").read()
    assert raw.startswith(storage.MAGIC) and b"secret" not in raw
    assert api.get(f"/api/documents/{f['id']}/file").content == b"Sales 27ABCPS1234K1Z5 secret"
