import io
import zipfile


def add(api, **kw):
    r = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5", "tds": True, "turnover_cr": 3} | kw,
                 headers=api.partner)
    assert r.status_code == 200, r.text
    return r.json()


def test_gstin_fills_profile_and_schedules(api):
    c = add(api)
    assert c["pan"] == "ABCPS1234K" and c["gst_state"] == "27" and c["gst_scheme"] == "monthly"
    assert c["entity_type"] == "individual"
    obls = api.get(f"/api/obligations?client_id={c['id']}").json()
    forms = {o["rule_code"] for o in obls}
    assert {"GSTR1_M", "GSTR3B_M", "GSTR9", "TDS_PAY", "ITR"} <= forms
    # anything due before onboarding is not dumped on the team as overdue
    assert all(o["due"] >= "2026-10-22" for o in obls)


def test_validation(api):
    r = api.post("/api/clients", json={"name": "X", "gstin": "27ABCPS1234K1Z5", "pan": "ZZZPZ9999Z"}, headers=api.partner)
    assert r.status_code == 400 and "doesn't match" in r.json()["detail"]
    add(api)
    assert api.post("/api/clients", json={"name": "Dup", "pan": "ABCPS1234K"}, headers=api.partner).status_code == 400


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


def test_upload_zip_classify_review_learn(api):
    c = add(api)
    api.post("/api/settings", json={"tracking_start": "2026-04-01"}, headers=api.partner)
    blob = zipped({
        "Sharma Traders/Sales register Sep-2026.csv": "Sales 27ABCPS1234K1Z5 sep",
        "Sharma Traders/Purchase register Sep-2026.csv": "Purchases 27ABCPS1234K1Z5 sep",
        "Sharma Traders/stuff/notes 2026-09.txt": "nothing obvious here",
        "zz_random_9.csv": "a,b\n1,2",
        "__MACOSX/._junk": "x",
    })
    r = api.post("/api/upload", files=[("files", ("dump.zip", blob, "application/zip"))], headers=api.staff).json()
    assert r["total"] == 4 and r["auto"] >= 2
    # duplicate upload is recognised
    r2 = api.post("/api/upload", files=[("files", ("dump.zip", blob, "application/zip"))], headers=api.staff).json()
    assert r2["duplicates"] == 4
    review = api.get("/api/documents?status=needs_review").json()
    rand = next(d for d in review if "zz_random" in d["filename"])
    ok = api.post(f"/api/documents/{rand['id']}/confirm",
                  json={"client_id": c["id"], "doc_type": "gstr2b", "period_start": "2026-09"}, headers=api.staff)
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    # learned: next file with the same token is matched to the client automatically
    r3 = api.post("/api/upload", files=[("files", ("zz_random_10 Oct 2026.csv", b"x,y\n3,4", "text/csv"))], headers=api.staff).json()
    assert r3["files"][0]["client_id"] == c["id"]
    sep = next(o for o in api.get(f"/api/obligations?client_id={c['id']}").json() if o["rule_code"] == "GSTR3B_M" and o["period_key"] == "2026-09")
    assert sep["readiness"] == 100


def test_workflow_guards(api):
    c = add(api, tds=False)
    o = next(o for o in api.get(f"/api/obligations?client_id={c['id']}").json() if o["rule_code"] == "GSTR1_M" and o["period_key"] == "2026-10")
    act = lambda a, h, **kw: api.post(f"/api/obligations/{o['id']}/action", json={"action": a} | kw, headers=h)
    assert act("approve", api.partner).status_code == 400  # must be in review first
    assert act("start", api.staff).status_code == 200
    assert act("submit", api.staff).status_code == 200
    r = act("approve", api.staff)
    assert r.status_code == 400 and "partner" in r.json()["detail"]
    assert act("approve", api.partner).status_code == 200
    assert act("file", api.staff).status_code == 400  # ack number required
    done = act("file", api.staff, ack_no="AA271026000001")
    assert done.status_code == 200 and done.json()["status"] == "filed"
    assert act("reopen", api.partner).status_code == 400  # needs a reason
    assert act("reopen", api.partner, note="Revised return").json()["status"] == "open"


def test_dependency_blocks_annual_return(api):
    c = add(api, tds=False)
    g9 = next(o for o in api.get(f"/api/obligations?client_id={c['id']}").json() if o["rule_code"] == "GSTR9" and o["fy"] == 2026)
    assert g9["blockers"]
    api.post(f"/api/obligations/{g9['id']}/action", json={"action": "start"}, headers=api.staff)
    r = api.post(f"/api/obligations/{g9['id']}/action", json={"action": "submit"}, headers=api.staff)
    assert r.status_code == 400 and "File these first" in r.json()["detail"]


def test_override_needs_partner(api):
    c = add(api)
    body = {"rule_code": "GSTR9", "fy": 2026, "period_key": "*", "new_due": "2028-01-31", "reason": "Notification X"}
    o = api.post("/api/overrides", json=body, headers=api.staff).json()
    assert api.post(f"/api/overrides/{o['id']}/approve", headers=api.staff).status_code == 403
    assert api.post(f"/api/overrides/{o['id']}/approve", headers=api.partner).status_code == 200
    g9 = next(x for x in api.get(f"/api/obligations?client_id={c['id']}").json() if x["rule_code"] == "GSTR9" and x["fy"] == 2026)
    assert g9["due"] == "2028-01-31" and g9["due_default"] == "2027-12-31" and g9["due_shifted"]


def test_chase_never_asks_for_running_month(api):
    c = add(api, tds=False)
    m = api.post(f"/api/clients/{c['id']}/chase", headers=api.staff)
    # Oct 2026 is still running on 22 Oct, so nothing to ask yet for a new client
    assert m.status_code == 400
    api.post("/api/settings", json={"tracking_start": "2026-04-01"}, headers=api.partner)
    c2 = add(api, name="Second", gstin="24AACCM4321Q1ZX", tds=False)
    msg = api.post(f"/api/clients/{c2['id']}/chase", headers=api.staff).json()
    assert "Sep 2026" in msg["body"] and "Oct 2026" not in msg["body"]
    assert msg["whatsapp_url"] is None
    assert api.post(f"/api/messages/{msg['id']}", json={"action": "sent"}, headers=api.staff).json()["status"] == "sent"


def test_brain_rules_and_proposals(api):
    a = api.post("/api/ask", json={"text": "add client Verma Exports Pvt Ltd 07AABCV1234D1Z3 audit tds payroll turnover 8 cr"},
                 headers=api.staff).json()
    p = a["proposals"][0]
    assert p["kind"] == "add_client" and p["payload"]["entity_type"] == "company" and p["payload"]["payroll"]
    assert p["payload"]["name"] == "Verma Exports Pvt Ltd" and p["payload"]["turnover_cr"] == 8
    # nothing happens until a human confirms
    assert api.get("/api/clients").json() == []
    denied = api.post("/api/proposals/execute", json=p, headers=api.staff)
    assert denied.status_code == 403  # Staff level can't add clients
    r = api.post("/api/proposals/execute", json=p).json()
    assert r["ok"] and r["client_id"]
    ext = api.post("/api/ask", json={"text": "extend GSTR-9 FY 2025-26 to 31 Jan 2027 per notification 9/2026"}, headers=api.staff).json()
    pl = ext["proposals"][0]["payload"]
    assert pl == {"rule_code": "GSTR9", "fy": 2025, "period_key": "*", "new_due": "2027-01-31", "reason": "notification 9/2026"}
    assert "approve" in ext["answer"]
    due = api.post("/api/ask", json={"text": "what's due this month"}, headers=api.staff).json()
    assert "due this month" in due["answer"]
    assert api.post("/api/ask", json={"text": "tell me a joke"}, headers=api.staff).json()["via"] == "rules"


def test_bulk_preview(api):
    rows = api.post("/api/clients/preview", json={"text": "Dr Anjali Rao BXRPR2345M salaried advance tax\nBad 27ZZZ\n"}).json()
    assert rows[0]["error"] is None and rows[0]["spec"]["salaried"] and rows[0]["spec"]["pan"] == "BXRPR2345M"


def test_demo_seed_end_to_end(api):
    assert api.post("/api/demo", headers=api.partner).status_code == 200
    d = api.get("/api/dashboard", headers=api.partner).json()
    assert d["counts"]["clients"] == 6 and d["counts"]["inbox"] <= 5 and d["counts"]["overdue"] >= 1
    assert api.post("/api/demo", headers=api.partner).status_code == 400
    team = {u["email"]: u for u in api.get("/api/users").json()}
    assert not team["priya@demo.local"]["custom_perms"] and team["priya@demo.local"]["perms"] == ["view_documents", "work"]
    assert team["arjun@demo.local"]["level"] == "manager" and team["arjun@demo.local"]["client_access"] == "all"
