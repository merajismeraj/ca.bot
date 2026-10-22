"""Firms, Google-only sign-in, super admins, levels and client-account access."""
import os

from conftest import bearer, login, make_firm


def test_no_password_login_anywhere(api):
    for path in ("/api/auth/login", "/api/auth/setup", "/api/auth/password"):
        assert api.post(path, json={}).status_code in (404, 405)


def test_firms_are_isolated(api):
    a_client = api.post("/api/clients", json={"name": "Sharma Traders", "pan": "ABCPS1234K"}).json()
    make_firm("Other & Co", owner="boss@other.in")
    b = login("boss@other.in")
    assert api.get("/api/clients", headers=b).json() == []
    assert api.get(f"/api/clients/{a_client['id']}", headers=b).status_code == 404
    # same PAN can exist in another firm's book - they are separate databases
    assert api.post("/api/clients", json={"name": "Sharma (B)", "pan": "ABCPS1234K"}, headers=b).status_code == 200
    assert [c["name"] for c in api.get("/api/clients").json()] == ["Sharma Traders"]
    assert len(api.get("/api/users", headers=b).json()) == 1


def test_member_of_two_firms_chooses(api):
    other = make_firm("Other & Co", owner="boss@other.in")
    api.post("/api/users", json={"name": "Neha", "email": "neha@x.in", "level": "manager"}, headers=login("boss@other.in"))
    h = login("neha@x.in")  # now in two firms: no automatic entry
    st = api.get("/api/auth/state", headers=h).json()
    assert {f["name"] for f in st["firms"]} == {"Test & Co", "Other & Co"} and st["firm_id"] is None
    assert api.get("/api/dashboard", headers=h).status_code == 409
    assert api.post("/api/auth/firm", json={"firm_id": other["id"]}, headers=h).status_code == 200
    assert api.get("/api/bootstrap", headers=h).json()["me"]["level"] == "manager"
    assert api.post("/api/auth/firm", json={"firm_id": 999}, headers=h).status_code == 404


def test_signup_needs_platform_approval(api, monkeypatch):
    monkeypatch.setenv("CABOT_PLATFORM_ADMINS", "owner@platform.in")
    h = login("ca@newfirm.in")
    r = api.post("/api/firms", json={"name": "New Firm & Associates", "accept_terms": True}, headers=h).json()
    assert r["pending"]
    fid = r["firm"]["id"]
    assert api.post("/api/auth/firm", json={"firm_id": fid}, headers=h).status_code == 403
    assert api.get("/api/platform/firms", headers=h).status_code == 403  # not a platform owner
    p = login("owner@platform.in")
    firms = api.get("/api/platform/firms", headers=p).json()
    assert {f["name"] for f in firms} >= {"Test & Co", "New Firm & Associates"}
    assert "clients" in firms[0] and "documents" not in firms[0]  # counts only, never client data
    api.post(f"/api/platform/firms/{fid}", json={"status": "active"}, headers=p)
    assert api.post("/api/auth/firm", json={"firm_id": fid}, headers=h).status_code == 200
    me = api.get("/api/bootstrap", headers=h).json()["me"]
    assert me["level"] == "owner" and "manage_team" in me["perms"]
    # the platform owner can suspend; sessions inside that firm end
    api.post(f"/api/platform/firms/{fid}", json={"status": "suspended"}, headers=p)
    assert api.get("/api/bootstrap", headers=h).status_code in (401, 403, 409)


def test_open_signup_and_platform_owner_cannot_read_firm_data(api, monkeypatch):
    monkeypatch.setenv("CABOT_OPEN_SIGNUP", "1")
    monkeypatch.setenv("CABOT_PLATFORM_ADMINS", "owner@platform.in")
    h = login("ca@open.in")
    r = api.post("/api/firms", json={"name": "Open Firm", "accept_terms": True}, headers=h).json()
    assert not r["pending"] and api.get("/api/bootstrap", headers=h).status_code == 200
    p = login("owner@platform.in")
    assert api.get("/api/clients", headers=p).status_code == 409  # platform owner isn't inside any firm


def test_platform_owner_sets_up_firm_for_customer(api, monkeypatch):
    monkeypatch.setenv("CABOT_PLATFORM_ADMINS", "owner@platform.in")
    p = login("owner@platform.in")
    f = api.post("/api/platform/firms", json={"name": "Customer CA", "owner_email": "Head@Customer.in"}, headers=p).json()
    assert f["status"] == "active"
    h = login("head@customer.in")  # first Google sign-in lands them in as super admin
    assert api.get("/api/bootstrap", headers=h).json()["me"]["level"] == "owner"


def test_levels_drive_permissions(api):
    a = api.post("/api/users", json={"name": "Asha", "email": "asha@x.in", "level": "article"}).json()
    v = api.post("/api/users", json={"name": "Vik", "email": "vik@x.in", "level": "viewer"}).json()
    assert set(a["perms"]) == {"view_documents", "work"} and a["client_access"] == "selected"
    assert v["perms"] == []
    c = api.post("/api/clients", json={"name": "Sharma Traders", "pan": "ABCPS1234K"}).json()
    hv = login("vik@x.in")
    api.patch(f"/api/users/{v['id']}", json={"client_ids": [c["id"]]})
    o = api.get(f"/api/obligations?client_id={c['id']}", headers=hv).json()[0]  # viewer can see filings
    assert api.post(f"/api/obligations/{o['id']}/action", json={"action": "start"}, headers=hv).status_code == 403
    assert api.post("/api/upload", files=[("files", ("a.csv", b"x", "text/csv"))], headers=hv).status_code == 403
    # firm tunes the Viewer level: give it document access
    api.post("/api/levels", json={"levels": {"viewer": ["view_documents"], "owner": []}})
    assert api.get("/api/bootstrap", headers=hv).json()["me"]["perms"] == ["view_documents"]
    assert api.get("/api/bootstrap").json()["me"]["level"] == "owner"  # owner level can't be reduced
    # per-person override, then back to the level
    api.patch(f"/api/users/{v['id']}", json={"perms": ["work"]})
    assert api.get("/api/bootstrap", headers=hv).json()["me"]["perms"] == ["work"]
    api.patch(f"/api/users/{v['id']}", json={"reset_perms": True})
    assert api.get("/api/bootstrap", headers=hv).json()["me"]["perms"] == ["view_documents"]


def test_selected_client_accounts(api):
    a = api.post("/api/clients", json={"name": "Alpha Co", "pan": "AAACA1111A"}).json()
    b = api.post("/api/clients", json={"name": "Beta Co", "pan": "AAACB2222B"}).json()
    api.patch(f"/api/users/{api.staff_id}", json={"client_access": "selected", "client_ids": [a["id"]]})
    assert [c["name"] for c in api.get("/api/clients", headers=api.staff).json()] == ["Alpha Co"]
    assert api.get(f"/api/clients/{b['id']}", headers=api.staff).status_code == 404
    assert {o["client_id"] for o in api.get("/api/obligations", headers=api.staff).json()} == {a["id"]}
    ans = api.post("/api/ask", json={"text": "status of Beta Co"}, headers=api.staff).json()
    assert all(i["client_name"] != "Beta Co" for i in ans["items"])
    other = api.get(f"/api/obligations?client_id={b['id']}").json()[0]
    assert api.post(f"/api/obligations/{other['id']}/action", json={"action": "start"}, headers=api.staff).status_code == 404
    team = {u["email"]: u for u in api.get("/api/users").json()}
    assert team["arjun@x.in"]["client_ids"] == [a["id"]]
    api.patch(f"/api/users/{api.staff_id}", json={"client_access": "all"})
    assert len(api.get("/api/clients", headers=api.staff).json()) == 2


def test_super_admin_rules(api):
    me = api.get("/api/bootstrap").json()["me"]
    r = api.patch(f"/api/users/{me['id']}", json={"level": "partner"})
    assert r.status_code == 400  # last super admin
    m = api.post("/api/users", json={"name": "Mo", "email": "mo@x.in", "level": "partner"}).json()
    api.patch(f"/api/users/{m['id']}", json={"perms": list(__import__("app.auth", fromlist=["PERMS"]).PERMS)})
    hm = login("mo@x.in")
    # a partner with Team permission still can't create or change super admins
    assert api.post("/api/users", json={"name": "Z", "email": "z@x.in", "level": "owner"}, headers=hm).status_code == 403
    assert api.patch(f"/api/users/{me['id']}", json={"active": False}, headers=hm).status_code == 403
    # staff can't manage the team at all
    assert api.post("/api/users", json={"name": "Q", "email": "q@x.in"}, headers=api.staff).status_code == 403
    assert api.post("/api/users", json={"name": "Dup", "email": "arjun@x.in"}).status_code == 400


def test_email_change_moves_access(api):
    api.patch(f"/api/users/{api.staff_id}", json={"email": "arjun.new@x.in"})
    assert api.get("/api/bootstrap", headers=api.staff).status_code == 401  # old Google identity no longer gets in
    assert api.get("/api/bootstrap", headers=login("arjun.new@x.in")).status_code == 200


def test_workspace_domain_enforced_on_invites(api, monkeypatch):
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAIN", "x.in")
    r = api.post("/api/users", json={"name": "G", "email": "g@gmail.com"})
    assert r.status_code == 400 and "@x.in" in r.json()["detail"]


def test_dev_login_is_localhost_only_and_off_by_default(api, monkeypatch):
    assert api.post("/api/auth/dev-login", json={"email": "neha@x.in"}).status_code == 404
    monkeypatch.setenv("CABOT_DEV_LOGIN", "1")
    assert api.post("/api/auth/dev-login", json={"email": "neha@x.in"}).status_code == 404  # TestClient isn't localhost


def test_single_office_upgrade(tmp_path, monkeypatch):
    """An install from before multi-firm (data/cabot.db) becomes firm #1 with its people and files intact."""
    import sqlite3
    from app import auth, db, tenancy
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    (tmp_path / "uploads" / "ab").mkdir(parents=True)
    (tmp_path / "uploads" / "ab" / "f.csv").write_text("x")
    con = sqlite3.connect(tmp_path / "cabot.db")
    con.executescript(db.SCHEMA)
    for t, cols in db.MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        for col, decl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {t} ADD COLUMN {col} {decl}")
    con.execute("INSERT INTO users(name, role, email, perms) VALUES(?,?,?,?),(?,?,?,?)",
                ("Neha", "partner", "neha@old.in", "[]", "Ravi", "staff", "ravi@old.in", '["send_messages", "all_clients"]'))
    con.execute("INSERT INTO documents(sha256, filename, stored_path, status) VALUES('s','f.csv',?, 'auto')",
                (str(tmp_path / "uploads" / "ab" / "f.csv"),))
    con.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("firm_name", '"Old Office"'))
    con.commit(); con.close()
    tenancy.init()
    f = tenancy.firm(1)
    assert f["name"] == "Old Office" and f["status"] == "active"
    assert {m["email"] for m in tenancy.memberships("neha@old.in")} == {"neha@old.in"}
    with db.use_firm(1), db.tx() as c:
        auth.ensure_levels(c)
        users = {u["email"]: u for u in db.rows(c, "SELECT * FROM users")}
        doc = db.row(c, "SELECT stored_path FROM documents")
    assert users["neha@old.in"]["level"] == "owner"
    assert users["ravi@old.in"]["level"] == "staff" and users["ravi@old.in"]["client_access"] == "all"
    assert os.path.exists(doc["stored_path"]) and "/firms/1/" in doc["stored_path"]
