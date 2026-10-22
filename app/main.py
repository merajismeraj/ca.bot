"""HTTP API + static UI. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, db, engine
from . import fy as F
from .catalog import DOC_TYPES, ENTITY_TYPES, GST_STATES, PAN_ENTITY
from .classifier import GSTIN_RE, PAN_RE
from .compliance import RULES, RULES_BY_CODE

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD = 200 * 1024 * 1024

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    with db.tx() as c:
        if not db.rows(c, "SELECT id FROM users"):
            c.execute("INSERT INTO users(name, role) VALUES ('Partner', 'partner'), ('Staff', 'staff')")
            db.set_setting(c, "tracking_start", engine.today().isoformat())
        # roll the calendar forward (new FY on 1 April, profile changes, approved extensions)
        engine.sync_obligations(c)
    yield


app = FastAPI(title="CA Brain", docs_url="/api/docs", lifespan=lifespan)


@app.exception_handler(engine.WorkflowError)
def _wf(_: Request, e: engine.WorkflowError):
    return JSONResponse({"detail": str(e)}, status_code=400)


def current_user(conn, uid: int | None) -> dict:
    u = db.row(conn, "SELECT * FROM users WHERE id=?", (uid,)) if uid else None
    return u or db.row(conn, "SELECT * FROM users ORDER BY role='partner' DESC, id LIMIT 1")


def require_partner(user: dict) -> None:
    if user["role"] != "partner":
        raise HTTPException(403, "Only a partner can do this")


# ================================================================ meta
@app.get("/api/bootstrap")
def bootstrap(x_user: int | None = Header(None)):
    with db.tx() as c:
        cur = F.fy_of(engine.today())
        signed = {r["fy"]: r for r in db.rows(c, "SELECT * FROM calendar_signoff")}
        return {
            "me": current_user(c, x_user),
            "users": db.rows(c, "SELECT * FROM users ORDER BY id"),
            "firm_name": db.get_setting(c, "firm_name", "Your CA Office"),
            "today": engine.today().isoformat(),
            "tracking_start": db.get_setting(c, "tracking_start", engine.today().isoformat()),
            "fys": [{"fy": f, "label": F.fy_label(f), "current": f == cur, "signed_off": f in signed}
                    for f in engine.live_fys()],
            "doc_types": {k: v["label"] for k, v in DOC_TYPES.items()},
            "entity_types": ENTITY_TYPES, "states": GST_STATES,
            "rules": [{"code": r.code, "name": r.name, "form": r.form, "authority": r.authority} for r in RULES],
            "llm": brain.llm_available(),
            "has_clients": bool(db.rows(c, "SELECT id FROM clients LIMIT 1")),
        }


class Settings(BaseModel):
    firm_name: str | None = None
    tracking_start: str | None = None


@app.post("/api/settings")
def save_settings(s: Settings, x_user: int | None = Header(None)):
    with db.tx() as c:
        require_partner(current_user(c, x_user))
        if s.firm_name is not None:
            db.set_setting(c, "firm_name", s.firm_name.strip() or "Your CA Office")
        if s.tracking_start:
            date.fromisoformat(s.tracking_start)
            db.set_setting(c, "tracking_start", s.tracking_start)
            # untouched filings follow the new start date; anything someone worked on stays as it is
            c.execute("UPDATE obligations SET status='open' WHERE status='not_tracked' AND due>=?", (s.tracking_start,))
            c.execute("UPDATE obligations SET status='not_tracked' WHERE status='open' AND due<?", (s.tracking_start,))
        return {"ok": True}


class NewUser(BaseModel):
    name: str
    role: str


@app.post("/api/users")
def add_user(u: NewUser, x_user: int | None = Header(None)):
    with db.tx() as c:
        require_partner(current_user(c, x_user))
        if u.role not in ("staff", "partner") or not u.name.strip():
            raise HTTPException(400, "Name and a role of staff/partner are required")
        cur = c.execute("INSERT INTO users(name, role) VALUES(?,?)", (u.name.strip(), u.role))
        return db.row(c, "SELECT * FROM users WHERE id=?", (cur.lastrowid,))


# =========================================================== dashboard
@app.get("/api/dashboard")
def dashboard(x_user: int | None = Header(None), mine: bool = False):
    t = engine.today()
    with db.tx() as c:
        me = current_user(c, x_user)
        obls = engine.enrich(c, db.rows(
            c, "SELECT * FROM obligations WHERE status NOT IN ('filed','na','not_tracked') AND due<=? ORDER BY due",
            ((t + timedelta(days=30)).isoformat(),)))
        if mine:
            obls = [o for o in obls if o["assignee_id"] in (None, me["id"])]
        counts = defaultdict(int)
        for o in obls:
            if o["days_left"] < 0:
                counts["overdue"] += 1
            elif o["days_left"] <= 7:
                counts["week"] += 1
            counts[o["stage"]] += 1
        # what this person should do, most urgent first; partner sees approvals at the top
        def rank(o):
            mine_first = 0 if (o["next_action"] and o["next_action"]["role"] == me["role"]) else 1
            return (mine_first if me["role"] == "partner" else 0, o["days_left"])
        obls.sort(key=rank)
        return {
            "counts": {
                "overdue": counts["overdue"], "week": counts["week"],
                "awaiting_docs": counts["awaiting_docs"], "review": counts["review"],
                "ready": counts["ready"], "in_progress": counts["in_progress"], "approved": counts["approved"],
                "inbox": db.row(c, "SELECT COUNT(*) n FROM documents WHERE status='needs_review'")["n"],
                "outbox": db.row(c, "SELECT COUNT(*) n FROM messages WHERE status IN ('draft','approved')")["n"],
                "overrides": db.row(c, "SELECT COUNT(*) n FROM due_overrides WHERE status='proposed'")["n"],
                "clients": db.row(c, "SELECT COUNT(*) n FROM clients WHERE archived=0")["n"],
            },
            "items": obls[:150],
            "filed_this_month": db.row(c, "SELECT COUNT(*) n FROM obligations WHERE status='filed' AND filed_on>=?",
                                       (t.replace(day=1).isoformat(),))["n"],
        }


@app.get("/api/obligations")
def obligations(client_id: int | None = None, fy: int | None = None, start: str | None = None,
                end: str | None = None, include_done: bool = True):
    sql, args = "SELECT * FROM obligations WHERE status!='not_tracked'", []
    for col, op, val in (("client_id", "=", client_id), ("fy", "=", fy), ("due", ">=", start), ("due", "<=", end)):
        if val is not None:
            sql += f" AND {col}{op}?"
            args.append(val)
    if not include_done:
        sql += " AND status NOT IN ('filed','na')"
    with db.tx() as c:
        return engine.enrich(c, db.rows(c, sql + " ORDER BY due, client_id", args))


class Action(BaseModel):
    action: str
    ack_no: str | None = None
    filed_on: str | None = None
    note: str | None = None


@app.post("/api/obligations/{oid}/action")
def obligation_action(oid: int, a: Action, x_user: int | None = Header(None)):
    with db.tx() as c:
        return engine.transition(c, oid, a.action, current_user(c, x_user), a.ack_no, a.filed_on, a.note)


class Assign(BaseModel):
    assignee_id: int | None


@app.post("/api/obligations/{oid}/assign")
def obligation_assign(oid: int, a: Assign, x_user: int | None = Header(None)):
    with db.tx() as c:
        c.execute("UPDATE obligations SET assignee_id=? WHERE id=?", (a.assignee_id, oid))
        return engine.enrich(c, [db.row(c, "SELECT * FROM obligations WHERE id=?", (oid,))])[0]


# ============================================================= calendar
@app.get("/api/calendar")
def calendar_view(month: str):
    """month = YYYY-MM. Returns per-day lists of obligations, grouped by form."""
    y, m = F.parse_month_key(month)
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    with db.tx() as c:
        obls = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE status!='not_tracked' AND due BETWEEN ? AND ? "
                                           "ORDER BY due", (start.isoformat(), end.isoformat())))
    days: dict[str, dict] = {}
    for o in obls:
        d = days.setdefault(o["due"], {"date": o["due"], "groups": {}})
        g = d["groups"].setdefault(o["form"], {"form": o["form"], "authority": o["authority"], "total": 0, "done": 0,
                                               "items": []})
        g["total"] += 1
        g["done"] += o["status"] in ("filed", "na")
        g["items"].append(o)
    return {"month": month, "days": [{**d, "groups": list(d["groups"].values())} for d in days.values()]}


class OverrideIn(BaseModel):
    rule_code: str
    fy: int
    period_key: str = "*"
    new_due: str
    reason: str


def _propose_override(c, o: OverrideIn, user: dict) -> dict:
    if o.rule_code not in RULES_BY_CODE:
        raise HTTPException(400, "Unknown compliance")
    date.fromisoformat(o.new_due)
    if not o.reason.strip():
        raise HTTPException(400, "Cite the notification / circular as the reason")
    cur = c.execute("INSERT INTO due_overrides(rule_code,fy,period_key,new_due,reason,status,proposed_by,created_at) "
                    "VALUES(?,?,?,?,?,'proposed',?,?)",
                    (o.rule_code, o.fy, o.period_key, o.new_due, o.reason.strip(), user["id"], db.now()))
    db.log(c, user["id"], "override_proposed", "override", cur.lastrowid,
           f"{user['name']} proposed {RULES_BY_CODE[o.rule_code].name} {F.fy_label(o.fy)} {o.period_key} → {o.new_due}")
    return db.row(c, "SELECT * FROM due_overrides WHERE id=?", (cur.lastrowid,))


@app.post("/api/overrides")
def propose_override(o: OverrideIn, x_user: int | None = Header(None)):
    with db.tx() as c:
        return _propose_override(c, o, current_user(c, x_user))


@app.get("/api/overrides")
def list_overrides():
    with db.tx() as c:
        out = db.rows(c, "SELECT o.*, u.name proposer FROM due_overrides o LEFT JOIN users u ON u.id=o.proposed_by "
                         "ORDER BY o.status='proposed' DESC, o.id DESC")
        for o in out:
            o["rule_name"] = RULES_BY_CODE[o["rule_code"]].name
            o["fy_label"] = F.fy_label(o["fy"])
            o["affected"] = db.row(c, "SELECT COUNT(*) n FROM obligations WHERE rule_code=? AND fy=? AND "
                                      "(period_key=? OR ?='*') AND status NOT IN ('filed','na')",
                                   (o["rule_code"], o["fy"], o["period_key"], o["period_key"]))["n"]
        return out


@app.post("/api/overrides/{oid}/{decision}")
def decide_override(oid: int, decision: str, x_user: int | None = Header(None)):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    with db.tx() as c:
        user = current_user(c, x_user)
        require_partner(user)
        o = db.row(c, "SELECT * FROM due_overrides WHERE id=?", (oid,))
        if not o or o["status"] != "proposed":
            raise HTTPException(400, "Already decided")
        c.execute("UPDATE due_overrides SET status=?, approved_by=? WHERE id=?",
                  ("approved" if decision == "approve" else "rejected", user["id"], oid))
        engine.sync_obligations(c)
        db.log(c, user["id"], f"override_{decision}", "override", oid,
               f"{user['name']} {decision}d due-date change for {RULES_BY_CODE[o['rule_code']].name} → {o['new_due']}")
        return {"ok": True}


@app.post("/api/calendar/{fy}/signoff")
def signoff(fy: int, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        require_partner(user)
        c.execute("INSERT OR REPLACE INTO calendar_signoff(fy, signed_by, signed_at) VALUES(?,?,?)", (fy, user["id"], db.now()))
        db.log(c, user["id"], "signoff", "calendar", fy, f"{user['name']} signed off the {F.fy_label(fy)} calendar")
        return {"ok": True}


@app.get("/api/rules")
def rules():
    return [{"code": r.code, "name": r.name, "form": r.form, "authority": r.authority, "help": r.help,
             "penalty": r.penalty, "depends_on": list(r.depends_on)} for r in RULES]


# ============================================================== clients
class ClientIn(BaseModel):
    name: str
    pan: str | None = None
    gstin: str | None = None
    tan: str | None = None
    entity_type: str | None = None
    gst_state: str | None = None
    gst_scheme: str = "none"
    turnover_cr: float | None = 0
    tax_audit: bool = False
    tds: bool = False
    tcs: bool = False
    payroll: bool = False
    advance_tax: bool = False
    presumptive: bool = False
    transfer_pricing: bool = False
    salaried: bool = False
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    assignee_id: int | None = None
    aliases: list[str] = []


def _clean_client(ci: ClientIn) -> dict:
    d = ci.model_dump()
    d["name"] = d["name"].strip()
    if not d["name"]:
        raise HTTPException(400, "Client name is required")
    for k in ("pan", "gstin", "tan"):
        d[k] = (d.get(k) or "").strip().upper() or None
    if d["gstin"]:
        m = GSTIN_RE.fullmatch(d["gstin"])
        if not m:
            raise HTTPException(400, "GSTIN doesn't look right (15 characters, e.g. 27ABCDE1234F1Z5)")
        if d["pan"] and d["pan"] != m.group(2):
            raise HTTPException(400, f"PAN {d['pan']} doesn't match the PAN inside the GSTIN ({m.group(2)})")
        d["pan"] = m.group(2)
        d["gst_state"] = d["gst_state"] or m.group(1)
        if d["gst_scheme"] == "none":
            d["gst_scheme"] = "monthly"
    if d["pan"] and not PAN_RE.fullmatch(d["pan"]):
        raise HTTPException(400, "PAN doesn't look right (e.g. ABCPS1234K)")
    if d["gst_scheme"] not in ("none", "monthly", "qrmp", "composition"):
        raise HTTPException(400, "Unknown GST scheme")
    d["entity_type"] = d["entity_type"] or (PAN_ENTITY.get(d["pan"][3]) if d["pan"] else None) or "individual"
    if d["entity_type"] not in ENTITY_TYPES:
        raise HTTPException(400, "Unknown entity type")
    if d["phone"]:
        d["phone"] = re.sub(r"\D", "", d["phone"])[-10:]
    d["aliases"] = json.dumps([a.strip() for a in d["aliases"] if a.strip()])
    return d


def _create_client(c, ci: ClientIn, user: dict) -> dict:
    d = _clean_client(ci)
    if d["pan"] and db.row(c, "SELECT id FROM clients WHERE pan=?", (d["pan"],)):
        raise HTTPException(400, f"A client with PAN {d['pan']} already exists")
    cols = list(d.keys()) + ["created_at"]
    cur = c.execute(f"INSERT INTO clients({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [d[k] for k in d] + [db.now()])
    cid = cur.lastrowid
    n = engine.sync_obligations(c, cid)
    db.log(c, user["id"], "client_add", "client", cid, f"{user['name']} added {d['name']} · {n} filings scheduled")
    # files dropped before the client existed may now be matchable
    _rematch_unassigned(c)
    return db.client_dict(db.row(c, "SELECT * FROM clients WHERE id=?", (cid,)))


def _rematch_unassigned(c) -> None:
    from . import classifier
    docs = db.rows(c, "SELECT * FROM documents WHERE status='needs_review' AND client_id IS NULL")
    if not docs:
        return
    clients = [db.client_dict(x) for x in db.rows(c, "SELECT * FROM clients WHERE archived=0")]
    lc, _ = engine._learned(c)
    for d in docs:
        cid, conf, why = classifier.match_client(d["filename"], d["text_excerpt"] or "", clients, lc, None)
        if not cid:
            continue
        confs = json.loads(d["confs"] or "{}") | {"client": conf}
        overall = round(min(confs.values()), 2)
        reasons = [f"Client: {why}"] + [r for r in json.loads(d["reasons"] or "[]") if not r.startswith("Client:")]
        c.execute("UPDATE documents SET client_id=?, confidence=?, confs=?, status=?, reasons=? WHERE id=?",
                  (cid, overall, json.dumps(confs), "auto" if overall >= classifier.AUTO_THRESHOLD else "needs_review",
                   json.dumps(reasons), d["id"]))


@app.get("/api/clients")
def list_clients():
    t = engine.today()
    with db.tx() as c:
        clients = [db.client_dict(x) for x in db.rows(c, "SELECT * FROM clients WHERE archived=0 ORDER BY name")]
        obls = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE status NOT IN ('filed','na','not_tracked') "
                                           "AND due<=?", ((t + timedelta(days=45)).isoformat(),)))
        docs = {r["client_id"]: r["n"] for r in db.rows(c, "SELECT client_id, COUNT(*) n FROM documents "
                                                           "WHERE status IN ('auto','confirmed') GROUP BY client_id")}
    by = defaultdict(list)
    for o in obls:
        by[o["client_id"]].append(o)
    for cl in clients:
        mine = by[cl["id"]]
        cl["overdue"] = sum(o["days_left"] < 0 for o in mine)
        cl["awaiting_docs"] = sum(o["stage"] == "awaiting_docs" for o in mine)
        cl["open"] = len(mine)
        cl["next_due"] = min((o["due"] for o in mine), default=None)
        cl["docs"] = docs.get(cl["id"], 0)
        cl["health"] = "red" if cl["overdue"] else ("amber" if cl["awaiting_docs"] else "green")
    return clients


@app.post("/api/clients")
def add_client(ci: ClientIn, x_user: int | None = Header(None)):
    with db.tx() as c:
        return _create_client(c, ci, current_user(c, x_user))


@app.put("/api/clients/{cid}")
def update_client(cid: int, ci: ClientIn, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        d = _clean_client(ci)
        if d["pan"] and db.row(c, "SELECT id FROM clients WHERE pan=? AND id!=?", (d["pan"], cid)):
            raise HTTPException(400, f"Another client already has PAN {d['pan']}")
        c.execute(f"UPDATE clients SET {','.join(k + '=?' for k in d)} WHERE id=?", [*d.values(), cid])
        engine.sync_obligations(c, cid)
        db.log(c, user["id"], "client_edit", "client", cid, f"{user['name']} updated {d['name']}'s profile")
        return db.client_dict(db.row(c, "SELECT * FROM clients WHERE id=?", (cid,)))


class BulkText(BaseModel):
    text: str


@app.post("/api/clients/preview")
def preview_clients(b: BulkText):
    """Plain-language client list -> parsed profiles, one per line, for a human to check before adding."""
    out = []
    with db.tx() as c:
        for line in b.text.splitlines():
            line = line.strip().strip(",;\t")
            if not line:
                continue
            spec = brain.parse_client_spec(line.replace("\t", " "))
            err = None
            try:
                cleaned = _clean_client(ClientIn(**{k: v for k, v in spec.items() if k in ClientIn.model_fields and v is not None}))
                if cleaned["pan"] and db.row(c, "SELECT id FROM clients WHERE pan=?", (cleaned["pan"],)):
                    err = f"Already a client (PAN {cleaned['pan']})"
            except HTTPException as e:
                err = e.detail
            except Exception:
                err = "Couldn't read this line"
            out.append({"line": line, "spec": spec, "error": err})
    return out


@app.get("/api/clients/{cid}")
def client_detail(cid: int):
    with db.tx() as c:
        cl = db.row(c, "SELECT * FROM clients WHERE id=?", (cid,))
        if not cl:
            raise HTTPException(404)
        obls = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE client_id=? AND status!='not_tracked' "
                                           "ORDER BY due", (cid,)))
        docs = [engine.doc_summary(d) for d in db.rows(c, "SELECT * FROM documents WHERE client_id=? AND status!='rejected' "
                                                          "ORDER BY doc_type, period_start", (cid,))]
        cov = engine.coverage(c, cid)
        activity = db.rows(c, "SELECT * FROM activity WHERE (entity='client' AND entity_id=?) OR (entity='obligation' "
                              "AND entity_id IN (SELECT id FROM obligations WHERE client_id=?)) ORDER BY id DESC LIMIT 40",
                           (cid, cid))
    # vault: FY -> doc type -> month grid
    vault = []
    for f in engine.live_fys():
        months = F.fy_months(f)
        rows_ = []
        for dt, meta in DOC_TYPES.items():
            have = [m for m in months if m in cov.get(dt, ())]
            ds = [d for d in docs if d["doc_type"] == dt and d["fy"] == f]
            if have or ds:
                rows_.append({"doc_type": dt, "label": meta["label"], "months": {m: m in have for m in months},
                              "docs": ds})
        vault.append({"fy": f, "label": F.fy_label(f), "months": [F.month_label(m)[:3] for m in months], "rows": rows_})
    return {"client": db.client_dict(cl), "obligations": obls, "vault": vault,
            "unsorted": [d for d in docs if d["status"] == "needs_review"], "activity": activity}


@app.delete("/api/clients/{cid}")
def archive_client(cid: int, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        require_partner(user)
        c.execute("UPDATE clients SET archived=1 WHERE id=?", (cid,))
        c.execute("DELETE FROM obligations WHERE client_id=? AND status IN ('open','not_tracked')", (cid,))
        db.log(c, user["id"], "client_archive", "client", cid, f"{user['name']} archived client #{cid}")
        return {"ok": True}


# ============================================================ documents
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), client_id: int | None = Form(None),
                 x_user: int | None = Header(None)):
    batch = uuid.uuid4().hex[:10]
    results = []
    total = 0
    with db.tx() as c:
        user = current_user(c, x_user)
        for f in files:
            data = await f.read()
            total += len(data)
            if total > MAX_UPLOAD:
                raise HTTPException(413, "Upload too large — split it into batches under 200 MB")
            try:
                parts = engine.expand_upload(f.filename or "upload", data)
            except Exception:
                results.append({"filename": f.filename, "skipped": True, "reason": "Could not open this zip"})
                continue
            for rel, blob in parts:
                results.append(engine.ingest(c, rel, blob, user, batch, client_id))
        engine.folder_consensus(c, batch)
        # re-read: consensus may have promoted files out of the review queue
        results = [db.row(c, "SELECT * FROM documents WHERE id=?", (r["id"],)) | {"duplicate": r.get("duplicate")}
                   if r.get("id") else r for r in results]
        filed = [r for r in results if r.get("status") == "auto" and not r.get("duplicate")]
        review = [r for r in results if r.get("status") == "needs_review" and not r.get("duplicate")]
        db.log(c, user["id"], "upload", "batch", None,
               f"{user['name']} uploaded {len(results)} files · {len(filed)} auto-filed · {len(review)} need a look")
        return {
            "batch": batch,
            "total": len(results),
            "auto": len(filed),
            "review": len(review),
            "duplicates": sum(1 for r in results if r.get("duplicate")),
            "skipped": [r for r in results if r.get("skipped")],
            "files": [engine.doc_summary(r) for r in results if not r.get("skipped")],
        }


@app.get("/api/documents")
def documents(status: str | None = None, client_id: int | None = None, batch: str | None = None):
    sql, args = "SELECT d.*, c.name client_name FROM documents d LEFT JOIN clients c ON c.id=d.client_id WHERE 1=1", []
    for col, val in (("d.status", status), ("d.client_id", client_id), ("d.batch_id", batch)):
        if val is not None:
            sql += f" AND {col}=?"
            args.append(val)
    with db.tx() as c:
        return [engine.doc_summary(d) for d in db.rows(c, sql + " ORDER BY d.id DESC LIMIT 500", args)]


class Confirm(BaseModel):
    client_id: int | None = None
    doc_type: str | None = None
    period_start: str | None = None
    period_end: str | None = None


@app.post("/api/documents/{did}/confirm")
def confirm(did: int, body: Confirm, x_user: int | None = Header(None)):
    with db.tx() as c:
        return engine.doc_summary(engine.confirm_document(c, did, current_user(c, x_user), body.client_id,
                                                          body.doc_type, body.period_start, body.period_end))


@app.post("/api/documents/{did}/reject")
def reject(did: int, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        c.execute("UPDATE documents SET status='rejected' WHERE id=?", (did,))
        db.log(c, user["id"], "doc_reject", "document", did, f"{user['name']} set aside a document")
        return {"ok": True}


@app.get("/api/documents/{did}/file")
def download(did: int):
    with db.tx() as c:
        d = db.row(c, "SELECT * FROM documents WHERE id=?", (did,))
    if not d or not Path(d["stored_path"]).exists():
        raise HTTPException(404)
    return FileResponse(d["stored_path"], filename=Path(d["filename"]).name)


# ============================================================== outbox
@app.post("/api/clients/{cid}/chase")
def chase(cid: int, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        draft = engine.build_chase(c, cid)
        if not draft:
            raise HTTPException(400, "Nothing pending from this client in the next 45 days")
        return _queue_message(c, cid, draft["subject"], draft["body"], user)


def _queue_message(c, cid: int, subject: str, body: str, user: dict) -> dict:
    existing = db.row(c, "SELECT id FROM messages WHERE client_id=? AND status='draft'", (cid,))
    if existing:
        c.execute("UPDATE messages SET body=?, subject=?, created_at=? WHERE id=?", (body, subject, db.now(), existing["id"]))
        mid = existing["id"]
    else:
        mid = c.execute("INSERT INTO messages(client_id,channel,subject,body,status,created_at) VALUES(?,?,?,?,'draft',?)",
                        (cid, "whatsapp", subject, body, db.now())).lastrowid
    db.log(c, user["id"], "chase_draft", "client", cid, f"{user['name']} drafted a document request")
    return _message(c, mid)


def _message(c, mid: int) -> dict:
    m = db.row(c, "SELECT m.*, c.name client_name, c.phone, c.email FROM messages m JOIN clients c ON c.id=m.client_id "
                  "WHERE m.id=?", (mid,))
    from urllib.parse import quote
    m["whatsapp_url"] = f"https://wa.me/91{m['phone']}?text={quote(m['body'])}" if m.get("phone") else None
    m["mailto_url"] = f"mailto:{m['email']}?subject={quote(m['subject'] or '')}&body={quote(m['body'])}" if m.get("email") else None
    return m


@app.get("/api/messages")
def messages():
    with db.tx() as c:
        return [_message(c, m["id"]) for m in db.rows(c, "SELECT id FROM messages WHERE status!='discarded' "
                                                         "ORDER BY status='sent', id DESC LIMIT 200")]


class MsgAction(BaseModel):
    action: str
    body: str | None = None


@app.post("/api/messages/{mid}")
def message_action(mid: int, a: MsgAction, x_user: int | None = Header(None)):
    with db.tx() as c:
        user = current_user(c, x_user)
        m = db.row(c, "SELECT * FROM messages WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        if a.body is not None and m["status"] == "draft":
            c.execute("UPDATE messages SET body=? WHERE id=?", (a.body, mid))
        if a.action == "approve":
            c.execute("UPDATE messages SET status='approved', approved_by=? WHERE id=?", (user["id"], mid))
        elif a.action == "sent":
            if m["status"] == "draft":
                c.execute("UPDATE messages SET approved_by=? WHERE id=?", (user["id"], mid))
            c.execute("UPDATE messages SET status='sent', sent_at=? WHERE id=?", (db.now(), mid))
            db.log(c, user["id"], "chase_sent", "client", m["client_id"], f"{user['name']} sent a document request")
        elif a.action == "discard":
            c.execute("UPDATE messages SET status='discarded' WHERE id=?", (mid,))
        elif a.action != "save":
            raise HTTPException(400, "Unknown action")
        return _message(c, mid)


# ================================================================ brain
class Ask(BaseModel):
    text: str
    history: list[dict[str, Any]] = []


@app.post("/api/ask")
def ask(q: Ask, x_user: int | None = Header(None)):
    with db.tx() as c:
        return brain.ask(c, q.text, current_user(c, x_user), q.history)


class Proposal(BaseModel):
    kind: str
    payload: dict[str, Any]


@app.post("/api/proposals/execute")
def execute(p: Proposal, x_user: int | None = Header(None)):
    """The single door through which the brain's suggestions become real - always human-clicked."""
    with db.tx() as c:
        user = current_user(c, x_user)
        if p.kind == "add_client":
            cl = _create_client(c, ClientIn(**{k: v for k, v in p.payload.items() if k in ClientIn.model_fields}), user)
            return {"ok": True, "message": f"Added {cl['name']} and scheduled their filings.", "client_id": cl["id"]}
        if p.kind == "override_due":
            o = _propose_override(c, OverrideIn(**p.payload), user)
            if user["role"] == "partner":
                c.execute("UPDATE due_overrides SET status='approved', approved_by=? WHERE id=?", (user["id"], o["id"]))
                engine.sync_obligations(c)
                return {"ok": True, "message": "Due date updated across all affected clients."}
            return {"ok": True, "message": "Sent to a partner for approval."}
        if p.kind == "transition":
            pl = p.payload
            e = engine.transition(c, int(pl["obligation_id"]), pl["action"], user, pl.get("ack_no"), None, pl.get("note"))
            return {"ok": True, "message": f"{e['form']} {e['period_label']} is now {e['stage'].replace('_', ' ')}."}
        if p.kind == "chase":
            cid = int(p.payload["client_id"])
            draft = engine.build_chase(c, cid)
            body = p.payload.get("body") or (draft and draft["body"])
            if not body:
                raise HTTPException(400, "Nothing to ask this client for")
            _queue_message(c, cid, (draft or {}).get("subject", "Documents needed"), body, user)
            return {"ok": True, "message": "Draft saved to the Outbox — review and send from there."}
        raise HTTPException(400, "Unknown proposal")


@app.get("/api/activity")
def activity():
    with db.tx() as c:
        return db.rows(c, "SELECT a.*, u.name actor_name FROM activity a LEFT JOIN users u ON u.id=a.actor "
                          "ORDER BY a.id DESC LIMIT 100")


@app.post("/api/demo")
def load_demo(x_user: int | None = Header(None)):
    from . import seed
    with db.tx() as c:
        if db.rows(c, "SELECT id FROM clients LIMIT 1"):
            raise HTTPException(400, "Demo data can only be loaded into an empty office")
    seed.run()
    return {"ok": True}


# ================================================================ static
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
