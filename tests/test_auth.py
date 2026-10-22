from conftest import bearer


def test_requires_login(api):
    assert api.get("/api/dashboard", headers={"Authorization": ""}).status_code == 401
    st = api.get("/api/auth/state", headers={"Authorization": ""}).json()
    assert st["setup_needed"] is False and st["user"] is None


def test_setup_only_once(api):
    r = api.post("/api/auth/setup", json={"firm_name": "X", "name": "Y", "email": "y@x.in", "password": "longenough"})
    assert r.status_code == 400


def test_login_errors_and_lockout(api):
    bad = lambda: api.post("/api/auth/login", json={"email": "neha@x.in", "password": "nope"}, headers={"Authorization": ""})
    for _ in range(5):
        assert bad().status_code == 401
    assert bad().status_code == 429
    ok = api.post("/api/auth/login", json={"email": "neha@x.in", "password": "partner-pass"}, headers={"Authorization": ""})
    assert ok.status_code == 429  # locked for this email even with the right password


def test_cookie_session_and_logout(api):
    api.headers.pop("Authorization")
    r = api.post("/api/auth/login", json={"email": "Neha@X.in ", "password": "partner-pass"})
    assert r.status_code == 200 and "cab_session" in r.cookies
    assert api.get("/api/bootstrap").json()["me"]["role"] == "partner"
    api.post("/api/auth/logout")
    assert api.get("/api/bootstrap").status_code == 401


def test_invite_change_password_and_deactivate(api):
    inv = api.invite(name="Priya", email="priya@x.in", perms=["send_messages"]).json()
    assert inv["user"]["must_change_pw"] and len(inv["temp_password"]) == 14
    tok = api.post("/api/auth/login", json={"email": "priya@x.in", "password": inv["temp_password"]}).json()["token"]
    h = bearer(tok)
    assert api.post("/api/auth/password", json={"current": "wrong", "new": "brand-new-pass"}, headers=h).status_code == 400
    assert api.post("/api/auth/password", json={"current": inv["temp_password"], "new": "short"}, headers=h).status_code == 400
    assert api.post("/api/auth/password", json={"current": inv["temp_password"], "new": "brand-new-pass"}, headers=h).status_code == 200
    assert not api.get("/api/bootstrap", headers=h).json()["me"]["must_change_pw"]
    api.patch(f"/api/users/{inv['user']['id']}", json={"active": False})
    assert api.get("/api/bootstrap", headers=h).status_code == 401  # sessions revoked


def test_last_partner_protected(api):
    me = api.get("/api/bootstrap").json()["me"]
    r = api.patch(f"/api/users/{me['id']}", json={"role": "staff"})
    assert r.status_code == 400 and "at least one active partner" in r.json()["detail"]


def test_staff_cannot_manage_team_or_approve(api):
    assert api.post("/api/users", json={"name": "Z", "email": "z@x.in"}, headers=api.staff).status_code == 403
    assert api.post("/api/settings", json={"firm_name": "Hack"}, headers=api.staff).status_code == 403
    assert api.post("/api/calendar/2026/signoff", headers=api.staff).status_code == 403


def test_permission_toggles(api):
    api.patch(f"/api/users/{api.staff_id}", json={"perms": ["send_messages"]})  # remove manage_clients
    r = api.post("/api/clients", json={"name": "Blocked"}, headers=api.staff)
    assert r.status_code == 403 and "clients" in r.json()["detail"]
    api.patch(f"/api/users/{api.staff_id}", json={"perms": ["approve"]})
    me = api.get("/api/bootstrap", headers=api.staff).json()["me"]
    assert me["perms"] == ["approve"]


def test_client_scope_for_restricted_staff(api):
    a = api.post("/api/clients", json={"name": "Assigned Co", "pan": "AAACA1111A", "assignee_id": api.staff_id}).json()
    b = api.post("/api/clients", json={"name": "Other Co", "pan": "AAACB2222B"}).json()
    api.patch(f"/api/users/{api.staff_id}", json={"perms": ["manage_clients", "send_messages"]})  # no all_clients
    names = [c["name"] for c in api.get("/api/clients", headers=api.staff).json()]
    assert names == ["Assigned Co"]
    assert api.get(f"/api/clients/{b['id']}", headers=api.staff).status_code == 404
    assert api.get(f"/api/clients/{a['id']}", headers=api.staff).status_code == 200
    assert {o["client_id"] for o in api.get("/api/obligations", headers=api.staff).json()} == {a["id"]}
    other = api.get(f"/api/obligations?client_id={b['id']}").json()[0]
    assert api.post(f"/api/obligations/{other['id']}/action", json={"action": "start"}, headers=api.staff).status_code == 404
    ans = api.post("/api/ask", json={"text": "status of Other Co"}, headers=api.staff).json()
    assert all(i["client_name"] != "Other Co" for i in ans["items"])
    # a client this person adds stays visible to them
    mine = api.post("/api/clients", json={"name": "New Mine", "pan": "AAACC3333C"}, headers=api.staff).json()
    assert mine["assignee_id"] == api.staff_id


def test_upgrade_from_passwordless_install(fresh):
    """An office that ran the first version (users without logins) upgrades in place."""
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    with db.tx() as c:
        c.execute("INSERT INTO users(name, role) VALUES ('Partner','partner'), ('Staff','staff')")
    with TestClient(app) as api:
        assert api.get("/api/auth/state").json()["setup_needed"]
        r = api.post("/api/auth/setup", json={"firm_name": "Old Co", "name": "Neha", "email": "n@x.in", "password": "longenough"})
        users = api.get("/api/users").json()
        assert r.json()["user"]["id"] == 1 and len(users) == 2  # the old partner record became the login
        staff = next(u for u in users if u["role"] == "staff")
        assert api.patch(f"/api/users/{staff['id']}", json={"email": "s@x.in"}).json()["email"] == "s@x.in"
        pw = api.post(f"/api/users/{staff['id']}/reset").json()["temp_password"]
        assert api.post("/api/auth/login", json={"email": "s@x.in", "password": pw}).status_code == 200
