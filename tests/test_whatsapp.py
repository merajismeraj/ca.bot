import hashlib
import hmac
import json

import pytest


@pytest.fixture
def wa(api, monkeypatch):
    for k, v in {"WA_TOKEN": "t", "WA_VERIFY_TOKEN": "verify-me", "WA_APP_SECRET": "s3cret"}.items():
        monkeypatch.setenv(k, v)
    assert api.post("/api/settings", json={"wa_phone_number_id": "123"}).status_code == 200
    api.client = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5", "phone": "98200 12345"}).json()
    return api


def signed(body: dict):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": sig, "content-type": "application/json", "Authorization": ""}


def msg(number="123", **m):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": number}, "contacts": [{"wa_id": "919820012345", "profile": {"name": "Rajesh"}}],
                                              "messages": [{"from": "919820012345", "timestamp": "1", **m}]}}]}]}


def test_verify_handshake(wa):
    ok = wa.get("/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=42", headers={"Authorization": ""})
    assert ok.status_code == 200 and ok.text == "42"
    assert wa.get("/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=1").status_code == 403


def test_rejects_unsigned(wa):
    raw = json.dumps(msg(id="m0", type="text", text={"body": "hi"})).encode()
    assert wa.post("/api/whatsapp/webhook", content=raw, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403


def test_inbound_text_and_document(wa, monkeypatch):
    from app import whatsapp
    raw, h = signed(msg(id="m1", type="text", text={"body": "Sending Sep purchases"}))
    assert wa.post("/api/whatsapp/webhook", content=raw, headers=h).status_code == 200
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)  # Meta retry: must not duplicate
    convo = wa.get(f"/api/clients/{wa.client['id']}").json()["conversation"]
    assert [m["body"] for m in convo] == ["Sending Sep purchases"]

    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (b"Purchase register\nGSTIN 27ABCPS1234K1Z5\n", "text/csv"))
    raw, h = signed(msg(id="m2", type="document",
                        document={"id": "MEDIA1", "filename": "purchase register sep 2026.csv", "mime_type": "text/csv"}))
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)
    docs = wa.get(f"/api/documents?client_id={wa.client['id']}").json()
    assert docs[0]["source"] == "whatsapp" and docs[0]["doc_type"] == "purchase_register" and docs[0]["status"] == "auto"
    assert docs[0]["filename"].startswith("WhatsApp/Sharma Traders/")
    assert wa.get("/api/dashboard").json()["counts"]["replies"] == 2


def test_unknown_sender_lands_unassigned(wa, monkeypatch):
    from app import whatsapp
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (b"some text", "application/pdf"))
    body = msg(id="m3", type="image", image={"id": "M", "mime_type": "image/jpeg", "caption": "bank stmt aug 2026"})
    body["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "917000000000"
    raw, h = signed(body)
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)
    d = wa.get("/api/documents?status=needs_review").json()[0]
    assert d["client_id"] is None and "bank stmt aug 2026" in d["filename"]


def test_send_respects_24h_window(wa, monkeypatch):
    from app import whatsapp
    api_post = []

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        def json(self): return {"messages": [{"id": "wamid.X"}]}
    monkeypatch.setattr(whatsapp.httpx, "post", lambda url, **kw: api_post.append((url, kw["json"])) or R())
    from app import db
    with db.tx() as c:
        mid = c.execute("INSERT INTO messages(client_id,channel,body,status,created_at,direction) VALUES(?,?,?,?,?,?)",
                        (wa.client["id"], "whatsapp", "Namaste\n• Purchase register — Sep 2026", "draft", db.now(), "out")).lastrowid
    r = wa.post(f"/api/messages/{mid}", json={"action": "send_whatsapp"})
    assert r.status_code == 400 and "24 hours" in r.json()["detail"] and not api_post
    raw, h = signed(msg(id="m9", type="text", text={"body": "ok"}))
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)
    r = wa.post(f"/api/messages/{mid}", json={"action": "send_whatsapp"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert api_post[0][1]["to"] == "919820012345" and api_post[0][1]["type"] == "text"


def test_template_outside_window(wa, monkeypatch):
    from app import db, whatsapp
    sent = []

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        def json(self): return {"messages": [{"id": "wamid.T"}]}
    monkeypatch.setattr(whatsapp.httpx, "post", lambda url, **kw: sent.append(kw["json"]) or R())
    monkeypatch.setenv("WA_TEMPLATE", "doc_request")
    with db.tx() as c:
        mid = c.execute("INSERT INTO messages(client_id,channel,body,status,created_at,direction) VALUES(?,?,?,?,?,?)",
                        (wa.client["id"], "whatsapp", "Hi\n• Purchase register — Sep 2026\n• GSTR-2B — Sep 2026", "draft",
                         db.now(), "out")).lastrowid
    assert wa.post(f"/api/messages/{mid}", json={"action": "send_whatsapp"}).status_code == 200
    t = sent[0]["template"]
    assert t["name"] == "doc_request"
    assert t["components"][0]["parameters"][0]["text"] == "Purchase register — Sep 2026 · GSTR-2B — Sep 2026"


def test_staff_without_send_permission(wa):
    from app import db
    wa.patch(f"/api/users/{wa.staff_id}", json={"perms": ["work", "view_documents"]})
    with db.tx() as c:
        mid = c.execute("INSERT INTO messages(client_id,channel,body,status,created_at,direction) VALUES(?,?,?,?,?,?)",
                        (wa.client["id"], "whatsapp", "x", "draft", db.now(), "out")).lastrowid
    assert wa.post(f"/api/messages/{mid}", json={"action": "sent"}, headers=wa.staff).status_code == 403


def test_messages_routed_to_the_firm_that_owns_the_number(wa, monkeypatch):
    """Two firms on one platform: a message to firm B's number must never land in firm A."""
    from conftest import login, make_firm
    from app import db
    other = make_firm("Other & Co", owner="boss@other.in")
    b = login("boss@other.in")
    wa.post("/api/settings", json={"wa_phone_number_id": "456"}, headers=b)
    assert wa.post("/api/settings", json={"wa_phone_number_id": "456"}).status_code == 400  # a number belongs to one firm
    raw, h = signed(msg(number="456", id="mB", type="text", text={"body": "for firm B"}))
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)
    raw, h = signed(msg(number="999", id="mX", type="text", text={"body": "unknown number"}))
    wa.post("/api/whatsapp/webhook", content=raw, headers=h)
    assert [m["body"] for m in wa.get("/api/messages").json()] == []
    assert [m["body"] for m in wa.get("/api/messages", headers=b).json()] == ["for firm B"]
