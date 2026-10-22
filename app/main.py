"""HTTP API + static UI. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel

from . import archive, auth, brain, db, engine, ocr, privacy, storage, whatsapp
from . import fy as F
from .catalog import DOC_TYPES, ENTITY_TYPES, GST_STATES, PAN_ENTITY
from .classifier import GSTIN_RE, PAN_RE
from .compliance import RULES, RULES_BY_CODE

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD = int(os.environ.get("CABOT_MAX_UPLOAD_MB", 4096)) * 1024 * 1024

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    with db.tx() as c:
        if db.get_setting(c, "tracking_start") is None:
            db.set_setting(c, "tracking_start", engine.today().isoformat())
        # roll the calendar forward (new FY on 1 April, profile changes, approved extensions)
        engine.sync_obligations(c)
    ocr.start_worker()
    yield


app = FastAPI(title="CA Brain", docs_url="/api/docs", lifespan=lifespan)


@app.exception_handler(engine.WorkflowError)
def _wf(_: Request, e: engine.WorkflowError):
    return JSONResponse({"detail": str(e)}, status_code=400)


def scope_sql(conn, user: dict, col: str = "client_id") -> tuple[str, list]:
    """SQL fragment limiting rows to the clients this person may see."""
    ids = auth.visible_client_ids(conn, user)
    if ids is None:
        return "", []
    if not ids:
        return " AND 0", []
    return f" AND {col} IN ({','.join('?' * len(ids))})", sorted(ids)


# ================================================================ auth
class SetupIn(BaseModel):
    firm_name: str
    name: str
    email: str
    password: str
    accept_terms: bool = False


class LoginIn(BaseModel):
    email: str
    password: str


def _login_response(c, user_id: int, response: Response) -> dict:
    token = auth.new_session(c, user_id)
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=auth.secure_cookies(),
                        max_age=auth.SESSION_DAYS * 86400, path="/")
    return {"ok": True, "token": token, "user": auth.public(db.row(c, "SELECT * FROM users WHERE id=?", (user_id,)))}


def _norm_email(e: str) -> str:
    e = (e or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise HTTPException(400, "Enter a valid email address")
    return e


@app.get("/api/auth/state")
def auth_state(request: Request):
    with db.tx() as c:
        u = auth.user_for_token(c, auth.token_from(request))
        return {"setup_needed": auth.setup_needed(c), "user": auth.public(u) if u else None,
                "firm_name": db.get_setting(c, "firm_name", "Your CA Office")}


@app.post("/api/auth/setup")
def auth_setup(body: SetupIn, response: Response):
    """First run only: create the office and its first partner."""
    with db.tx() as c:
        if not auth.setup_needed(c):
            raise HTTPException(400, "This office is already set up — sign in instead")
        if not body.accept_terms:
            raise HTTPException(400, "Please accept the Terms of Use to continue")
        auth.validate_pw(body.password)
        if not body.name.strip():
            raise HTTPException(400, "Your name is required")
        email = _norm_email(body.email)
        db.set_setting(c, "firm_name", body.firm_name.strip() or "Your CA Office")
        # an older install has users without passwords: the first partner becomes this login
        legacy = db.row(c, "SELECT id FROM users WHERE role='partner' AND pw_hash IS NULL ORDER BY id LIMIT 1")
        if legacy:
            c.execute("UPDATE users SET name=?, email=?, pw_hash=?, active=1 WHERE id=?",
                      (body.name.strip(), email, auth.hash_pw(body.password), legacy["id"]))
            uid = legacy["id"]
        else:
            uid = c.execute("INSERT INTO users(name, role, email, pw_hash, perms, active) VALUES(?,?,?,?,?,1)",
                            (body.name.strip(), "partner", email, auth.hash_pw(body.password), "[]")).lastrowid
        db.set_setting(c, "terms_accepted", {"by": uid, "name": body.name.strip(), "email": email, "at": db.now()})
        db.log(c, uid, "setup", "office", None, f"{body.name.strip()} set up the office and accepted the Terms of Use")
        return _login_response(c, uid, response)


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response, request: Request):
    key = (body.email or "").strip().lower()
    auth.check_rate(key)
    auth.check_rate(f"ip:{request.client.host if request.client else '-'}")
    with db.tx() as c:
        u = db.row(c, "SELECT * FROM users WHERE email=? AND active=1", (key,))
        if not u or not auth.check_pw(body.password, u["pw_hash"]):
            auth.record_fail(key)
            auth.record_fail(f"ip:{request.client.host if request.client else '-'}")
            raise HTTPException(401, "Email or password is wrong")
        db.log(c, u["id"], "login", "user", u["id"], f"{u['name']} signed in")
        return _login_response(c, u["id"], response)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    with db.tx() as c:
        auth.end_session(c, auth.token_from(request))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user: dict = Depends(auth.current)):
    if not auth.check_pw(body.current, user["pw_hash"]):
        raise HTTPException(400, "Current password is wrong")
    auth.validate_pw(body.new)
    with db.tx() as c:
        c.execute("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?", (auth.hash_pw(body.new), user["id"]))
        # sign out every other device
        keep = auth._digest(auth.token_from(request) or "")
        c.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user["id"], keep))
    return {"ok": True}


# ================================================================ meta
@app.get("/api/bootstrap")
def bootstrap(user: dict = Depends(auth.current)):
    with db.tx() as c:
        cur = F.fy_of(engine.today())
        signed = {r["fy"]: r for r in db.rows(c, "SELECT * FROM calendar_signoff")}
        return {
            "me": auth.public(user),
            "users": [auth.public(u) for u in db.rows(c, "SELECT * FROM users WHERE active=1 ORDER BY id")],
            "perms": auth.PERMS,
            "whatsapp": whatsapp.configured(),
            "ocr": {"tesseract": ocr.tesseract_available(), "vision": ocr.vision_enabled()},
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
            "can_load_demo": not db.rows(c, "SELECT id FROM clients LIMIT 1") and auth.can(user, "manage_team"),
        }


class Settings(BaseModel):
    firm_name: str | None = None
    tracking_start: str | None = None


@app.post("/api/settings")
def save_settings(s: Settings, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    with db.tx() as c:
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
    email: str
    role: str = "staff"
    perms: list[str] | None = None
    phone: str | None = None


class UserPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    perms: list[str] | None = None
    active: bool | None = None
    phone: str | None = None


@app.get("/api/users")
def list_users(user: dict = Depends(auth.current)):
    with db.tx() as c:
        return [auth.public(u) for u in db.rows(c, "SELECT * FROM users ORDER BY active DESC, role='partner' DESC, name")]


@app.post("/api/users")
def add_user(u: NewUser, user: dict = Depends(auth.current)):
    """Invite a colleague. Returns a one-time password to hand over; they must change it on first sign-in."""
    auth.require(user, "manage_team")
    if u.role not in ("staff", "partner") or not u.name.strip():
        raise HTTPException(400, "Name and a role of staff/partner are required")
    email = _norm_email(u.email)
    perms = [p for p in (u.perms if u.perms is not None else auth.STAFF_DEFAULT) if p in auth.PERMS]
    temp = auth.temp_password()
    with db.tx() as c:
        if db.row(c, "SELECT id FROM users WHERE email=?", (email,)):
            raise HTTPException(400, "Someone with this email is already on the team")
        uid = c.execute("INSERT INTO users(name, role, email, pw_hash, perms, active, must_change_pw, phone) "
                        "VALUES(?,?,?,?,?,1,1,?)", (u.name.strip(), u.role, email, auth.hash_pw(temp), json.dumps(perms),
                                                    re.sub(r"\D", "", u.phone or "")[-10:] or None)).lastrowid
        db.log(c, user["id"], "user_add", "user", uid, f"{user['name']} invited {u.name.strip()} ({u.role})")
        return {"user": auth.public(db.row(c, "SELECT * FROM users WHERE id=?", (uid,))), "temp_password": temp}


@app.patch("/api/users/{uid}")
def edit_user(uid: int, p: UserPatch, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    with db.tx() as c:
        target = db.row(c, "SELECT * FROM users WHERE id=?", (uid,))
        if not target:
            raise HTTPException(404)
        new_role = p.role or target["role"]
        new_active = target["active"] if p.active is None else int(p.active)
        if new_role not in ("staff", "partner"):
            raise HTTPException(400, "Role must be staff or partner")
        if target["role"] == "partner" and (new_role != "partner" or not new_active):
            others = db.row(c, "SELECT COUNT(*) n FROM users WHERE role='partner' AND active=1 AND id!=?", (uid,))["n"]
            if not others:
                raise HTTPException(400, "The office needs at least one active partner")
        perms = json.dumps([x for x in p.perms if x in auth.PERMS]) if p.perms is not None else target["perms"]
        email = _norm_email(p.email) if p.email is not None else None
        if email and db.row(c, "SELECT id FROM users WHERE email=? AND id!=?", (email, uid)):
            raise HTTPException(400, "Someone else already uses this email")
        c.execute("UPDATE users SET name=COALESCE(?,name), email=COALESCE(?,email), role=?, perms=?, active=?, "
                  "phone=COALESCE(?,phone) WHERE id=?", (p.name, email, new_role, perms, new_active, p.phone, uid))
        if not new_active:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.log(c, user["id"], "user_edit", "user", uid, f"{user['name']} updated {target['name']}'s access")
        return auth.public(db.row(c, "SELECT * FROM users WHERE id=?", (uid,)))


@app.post("/api/users/{uid}/reset")
def reset_password(uid: int, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    temp = auth.temp_password()
    with db.tx() as c:
        t = db.row(c, "SELECT * FROM users WHERE id=?", (uid,))
        if not t:
            raise HTTPException(404)
        c.execute("UPDATE users SET pw_hash=?, must_change_pw=1 WHERE id=?", (auth.hash_pw(temp), uid))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.log(c, user["id"], "user_reset", "user", uid, f"{user['name']} reset {t['name']}'s password")
    return {"temp_password": temp}


# =========================================================== dashboard
@app.get("/api/dashboard")
def dashboard(user: dict = Depends(auth.current), mine: bool = False):
    t = engine.today()
    with db.tx() as c:
        sc, sa = scope_sql(c, user)
        obls = engine.enrich(c, db.rows(
            c, "SELECT * FROM obligations WHERE status NOT IN ('filed','na','not_tracked') AND due<=?" + sc + " ORDER BY due",
            ((t + timedelta(days=30)).isoformat(), *sa)))
        if mine:
            obls = [o for o in obls if o["assignee_id"] in (None, user["id"])]
        counts = defaultdict(int)
        for o in obls:
            if o["days_left"] < 0:
                counts["overdue"] += 1
            elif o["days_left"] <= 7:
                counts["week"] += 1
            counts[o["stage"]] += 1
        approver = auth.can(user, "approve")

        # what this person should do, most urgent first; approvers see approvals at the top
        def rank(o):
            needs_me = o["next_action"] and o["next_action"]["role"] == "partner"
            return (0 if (approver and needs_me) else 1, o["days_left"])
        obls.sort(key=rank)
        dsc, dsa = scope_sql(c, user)
        return {
            "counts": {
                "overdue": counts["overdue"], "week": counts["week"],
                "awaiting_docs": counts["awaiting_docs"], "review": counts["review"],
                "ready": counts["ready"], "in_progress": counts["in_progress"], "approved": counts["approved"],
                "inbox": db.row(c, "SELECT COUNT(*) n FROM documents WHERE status='needs_review' AND "
                                   "(client_id IS NULL" + (dsc.replace(" AND", " OR", 1) if dsc else " OR 1") + ")", dsa)["n"],
                "reading": db.row(c, "SELECT COUNT(*) n FROM documents WHERE ocr_status IN ('pending','running')")["n"],
                "outbox": db.row(c, "SELECT COUNT(*) n FROM messages WHERE direction='out' AND status IN ('draft','approved')"
                                    + sc, sa)["n"],
                "replies": db.row(c, "SELECT COUNT(*) n FROM messages WHERE direction='in' AND status='received'" + sc, sa)["n"],
                "overrides": db.row(c, "SELECT COUNT(*) n FROM due_overrides WHERE status='proposed'")["n"],
                "clients": db.row(c, "SELECT COUNT(*) n FROM clients WHERE archived=0" + scope_sql(c, user, "id")[0],
                                  scope_sql(c, user, "id")[1])["n"],
            },
            "items": obls[:150],
            "filed_this_month": db.row(c, "SELECT COUNT(*) n FROM obligations WHERE status='filed' AND filed_on>=?" + sc,
                                       (t.replace(day=1).isoformat(), *sa))["n"],
        }


@app.get("/api/obligations")
def obligations(client_id: int | None = None, fy: int | None = None, start: str | None = None,
                end: str | None = None, include_done: bool = True, user: dict = Depends(auth.current)):
    sql, args = "SELECT * FROM obligations WHERE status!='not_tracked'", []
    for col, op, val in (("client_id", "=", client_id), ("fy", "=", fy), ("due", ">=", start), ("due", "<=", end)):
        if val is not None:
            sql += f" AND {col}{op}?"
            args.append(val)
    if not include_done:
        sql += " AND status NOT IN ('filed','na')"
    with db.tx() as c:
        sc, sa = scope_sql(c, user)
        return engine.enrich(c, db.rows(c, sql + sc + " ORDER BY due, client_id", [*args, *sa]))


def _obligation_for(c, oid: int, user: dict) -> dict:
    o = db.row(c, "SELECT * FROM obligations WHERE id=?", (oid,))
    if not o:
        raise HTTPException(404, "Filing not found")
    auth.require_client(c, user, o["client_id"])
    return o


class Action(BaseModel):
    action: str
    ack_no: str | None = None
    filed_on: str | None = None
    note: str | None = None


@app.post("/api/obligations/{oid}/action")
def obligation_action(oid: int, a: Action, user: dict = Depends(auth.current)):
    with db.tx() as c:
        _obligation_for(c, oid, user)
        return engine.transition(c, oid, a.action, user, a.ack_no, a.filed_on, a.note)


class Assign(BaseModel):
    assignee_id: int | None


@app.post("/api/obligations/{oid}/assign")
def obligation_assign(oid: int, a: Assign, user: dict = Depends(auth.current)):
    with db.tx() as c:
        o = _obligation_for(c, oid, user)
        if a.assignee_id and not db.row(c, "SELECT id FROM users WHERE id=? AND active=1", (a.assignee_id,)):
            raise HTTPException(400, "Unknown team member")
        c.execute("UPDATE obligations SET assignee_id=? WHERE id=?", (a.assignee_id, oid))
        db.log(c, user["id"], "assign", "obligation", oid, f"{user['name']} reassigned {o['rule_code']} {o['period_key']}")
        return engine.enrich(c, [db.row(c, "SELECT * FROM obligations WHERE id=?", (oid,))])[0]


# ============================================================= calendar
@app.get("/api/calendar")
def calendar_view(month: str, user: dict = Depends(auth.current)):
    """month = YYYY-MM. Returns per-day lists of obligations, grouped by form."""
    y, m = F.parse_month_key(month)
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    with db.tx() as c:
        sc, sa = scope_sql(c, user)
        obls = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE status!='not_tracked' AND due BETWEEN ? AND ?"
                                           + sc + " ORDER BY due", (start.isoformat(), end.isoformat(), *sa)))
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
    try:
        date.fromisoformat(o.new_due)
    except ValueError:
        raise HTTPException(400, "New due date must be a date")
    if not o.reason.strip():
        raise HTTPException(400, "Cite the notification / circular as the reason")
    cur = c.execute("INSERT INTO due_overrides(rule_code,fy,period_key,new_due,reason,status,proposed_by,created_at) "
                    "VALUES(?,?,?,?,?,'proposed',?,?)",
                    (o.rule_code, o.fy, o.period_key, o.new_due, o.reason.strip(), user["id"], db.now()))
    db.log(c, user["id"], "override_proposed", "override", cur.lastrowid,
           f"{user['name']} proposed {RULES_BY_CODE[o.rule_code].name} {F.fy_label(o.fy)} {o.period_key} → {o.new_due}")
    return db.row(c, "SELECT * FROM due_overrides WHERE id=?", (cur.lastrowid,))


@app.post("/api/overrides")
def propose_override(o: OverrideIn, user: dict = Depends(auth.current)):
    with db.tx() as c:
        return _propose_override(c, o, user)


@app.get("/api/overrides")
def list_overrides(user: dict = Depends(auth.current)):
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
def decide_override(oid: int, decision: str, user: dict = Depends(auth.current)):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    auth.require(user, "approve")
    with db.tx() as c:
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
def signoff(fy: int, user: dict = Depends(auth.current)):
    auth.require(user, "approve")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO calendar_signoff(fy, signed_by, signed_at) VALUES(?,?,?)", (fy, user["id"], db.now()))
        db.log(c, user["id"], "signoff", "calendar", fy, f"{user['name']} signed off the {F.fy_label(fy)} calendar")
        return {"ok": True}


@app.get("/api/rules")
def rules(user: dict = Depends(auth.current)):
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
    auth.require(user, "manage_clients")
    d = _clean_client(ci)
    if d["pan"] and db.row(c, "SELECT id FROM clients WHERE pan=?", (d["pan"],)):
        raise HTTPException(400, f"A client with PAN {d['pan']} already exists")
    if not auth.can(user, "all_clients") and not d["assignee_id"]:
        d["assignee_id"] = user["id"]  # otherwise the person who added it couldn't see it
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
def list_clients(user: dict = Depends(auth.current)):
    t = engine.today()
    with db.tx() as c:
        ic, ia = scope_sql(c, user, "id")
        sc, sa = scope_sql(c, user)
        clients = [db.client_dict(x) for x in db.rows(c, "SELECT * FROM clients WHERE archived=0" + ic + " ORDER BY name", ia)]
        obls = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE status NOT IN ('filed','na','not_tracked') "
                                           "AND due<=?" + sc, ((t + timedelta(days=45)).isoformat(), *sa)))
        docs = {r["client_id"]: r["n"] for r in db.rows(c, "SELECT client_id, COUNT(*) n FROM documents "
                                                           "WHERE status IN ('auto','confirmed') GROUP BY client_id")}
        replies = {r["client_id"]: r["n"] for r in db.rows(c, "SELECT client_id, COUNT(*) n FROM messages "
                                                              "WHERE direction='in' AND status='received' GROUP BY client_id")}
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
        cl["replies"] = replies.get(cl["id"], 0)
        cl["health"] = "red" if cl["overdue"] else ("amber" if cl["awaiting_docs"] else "green")
    return clients


@app.post("/api/clients")
def add_client(ci: ClientIn, user: dict = Depends(auth.current)):
    with db.tx() as c:
        return _create_client(c, ci, user)


@app.put("/api/clients/{cid}")
def update_client(cid: int, ci: ClientIn, user: dict = Depends(auth.current)):
    auth.require(user, "manage_clients")
    with db.tx() as c:
        auth.require_client(c, user, cid)
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
def preview_clients(b: BulkText, user: dict = Depends(auth.current)):
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
def client_detail(cid: int, user: dict = Depends(auth.current)):
    with db.tx() as c:
        auth.require_client(c, user, cid)
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
        convo = db.rows(c, "SELECT id, direction, body, status, created_at, sent_at FROM messages WHERE client_id=? "
                           "AND status!='discarded' ORDER BY id DESC LIMIT 30", (cid,))
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
    return {"client": db.client_dict(cl), "obligations": obls, "vault": vault, "conversation": convo,
            "unsorted": [d for d in docs if d["status"] == "needs_review"], "activity": activity}


@app.delete("/api/clients/{cid}")
def archive_client(cid: int, user: dict = Depends(auth.current)):
    auth.require(user, "manage_clients")
    auth.require(user, "approve")
    with db.tx() as c:
        c.execute("UPDATE clients SET archived=1 WHERE id=?", (cid,))
        c.execute("DELETE FROM obligations WHERE client_id=? AND status IN ('open','not_tracked')", (cid,))
        db.log(c, user["id"], "client_archive", "client", cid, f"{user['name']} archived client #{cid}")
        return {"ok": True}


# ============================================================ documents
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), client_id: int | None = Form(None),
                 user: dict = Depends(auth.current)):
    """Accepts any mix of files and zips (nested, Windows-made, WhatsApp exports). Zips are read entry by entry
    from the spooled upload, never fully into memory."""
    batch = uuid.uuid4().hex[:10]
    results: list[dict] = []
    total = 0
    with db.tx() as c:
        if client_id:
            auth.require_client(c, user, client_id)
            if privacy.withdrawn(c, client_id):
                raise HTTPException(400, "This client has withdrawn consent — new documents can't be added (DPDP s.6(6))")
        for f in files:
            name = (f.filename or "upload").replace("\\", "/")
            head = await f.read(4)
            await f.seek(0)
            if archive.is_zip(name, head):
                f.file.seek(0, 2)
                total += f.file.tell()
                f.file.seek(0)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "Upload too large — split it into smaller zips")
                for e in archive.expand(name, f.file):
                    if e.skipped:
                        results.append({"filename": e.path, "skipped": True, "reason": e.skipped})
                    else:
                        results.append(engine.ingest(c, e.path, e.data, user, batch, client_id))
                continue
            data = await f.read()
            total += len(data)
            if total > MAX_UPLOAD:
                raise HTTPException(413, "Upload too large — split it into smaller batches")
            if len(data) > archive.MAX_FILE:
                results.append({"filename": name, "skipped": True, "reason": "Larger than 100 MB"})
                continue
            results.append(engine.ingest(c, name, data, user, batch, client_id))
        engine.folder_consensus(c, batch)
        # re-read: consensus may have promoted files out of the review queue
        results = [db.row(c, "SELECT * FROM documents WHERE id=?", (r["id"],)) | {"duplicate": r.get("duplicate")}
                   if r.get("id") else r for r in results]
        fresh = [r for r in results if r.get("id") and not r.get("duplicate")]
        filed = [r for r in fresh if r["status"] == "auto"]
        review = [r for r in fresh if r["status"] == "needs_review"]
        reading = [r for r in fresh if r.get("ocr_status") == "pending"]
        skipped = [r for r in results if r.get("skipped")]
        db.log(c, user["id"], "upload", "batch", None,
               f"{user['name']} uploaded {len(results)} files · {len(filed)} auto-filed · {len(review)} need a look")
        return {
            "batch": batch,
            "total": len(results),
            "auto": len(filed),
            "review": len(review),
            "reading": len(reading),
            "duplicates": sum(1 for r in results if r.get("duplicate")),
            "skipped": [{"filename": r["filename"], "reason": r["reason"]} for r in skipped],
            "files": [engine.doc_summary(r) for r in results if r.get("id")][:500],
        }


@app.get("/api/documents")
def documents(status: str | None = None, client_id: int | None = None, batch: str | None = None,
              user: dict = Depends(auth.current)):
    sql, args = "SELECT d.*, c.name client_name FROM documents d LEFT JOIN clients c ON c.id=d.client_id WHERE 1=1", []
    for col, val in (("d.status", status), ("d.client_id", client_id), ("d.batch_id", batch)):
        if val is not None:
            sql += f" AND {col}=?"
            args.append(val)
    with db.tx() as c:
        sc, sa = scope_sql(c, user, "d.client_id")
        if sc:  # restricted staff also see unassigned files, so the Inbox can still be worked
            sql += " AND (d.client_id IS NULL" + sc.replace(" AND", " OR", 1) + ")"
            args += sa
        return [engine.doc_summary(d) for d in db.rows(c, sql + " ORDER BY d.id DESC LIMIT 500", args)]


def _doc_for(c, did: int, user: dict) -> dict:
    d = db.row(c, "SELECT * FROM documents WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "Document not found")
    auth.require_client(c, user, d["client_id"])
    return d


class Confirm(BaseModel):
    client_id: int | None = None
    doc_type: str | None = None
    period_start: str | None = None
    period_end: str | None = None


@app.post("/api/documents/{did}/confirm")
def confirm(did: int, body: Confirm, user: dict = Depends(auth.current)):
    with db.tx() as c:
        _doc_for(c, did, user)
        auth.require_client(c, user, body.client_id)
        return engine.doc_summary(engine.confirm_document(c, did, user, body.client_id,
                                                          body.doc_type, body.period_start, body.period_end))


@app.post("/api/documents/{did}/reject")
def reject(did: int, user: dict = Depends(auth.current)):
    with db.tx() as c:
        _doc_for(c, did, user)
        c.execute("UPDATE documents SET status='rejected' WHERE id=?", (did,))
        db.log(c, user["id"], "doc_reject", "document", did, f"{user['name']} set aside a document")
        return {"ok": True}


@app.get("/api/documents/{did}/file")
def download(did: int, user: dict = Depends(auth.current)):
    with db.tx() as c:
        d = _doc_for(c, did, user)
    if not d["stored_path"] or not Path(d["stored_path"]).exists():
        raise HTTPException(404, "This file has been erased")
    with db.tx() as c:
        db.log(c, user["id"], "doc_view", "document", did, f"{user['name']} opened {Path(d['filename']).name}")
    import mimetypes
    from urllib.parse import quote
    name = Path(d["filename"]).name
    return Response(storage.read(d["stored_path"]), media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _zip_response(c, client_ids: list[int], name: str, user: dict) -> FileResponse:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=db.DATA_DIR)
    with tmp:
        n = archive.export_clients(c, client_ids, tmp)
    db.log(c, user["id"], "export", "client", client_ids[0] if len(client_ids) == 1 else None,
           f"{user['name']} downloaded {n} files as a zip")
    return FileResponse(tmp.name, filename=name, media_type="application/zip",
                        background=BackgroundTask(os.unlink, tmp.name))


@app.get("/api/clients/{cid}/export")
def export_client(cid: int, user: dict = Depends(auth.current)):
    auth.require(user, "export")
    with db.tx() as c:
        auth.require_client(c, user, cid)
        cl = db.row(c, "SELECT name FROM clients WHERE id=?", (cid,))
        if not cl:
            raise HTTPException(404)
        return _zip_response(c, [cid], f"{archive._safe(cl['name'])} - {engine.today():%Y-%m-%d}.zip", user)


@app.get("/api/export")
def export_all(user: dict = Depends(auth.current)):
    auth.require(user, "export")
    with db.tx() as c:
        ic, ia = scope_sql(c, user, "id")
        ids = [r["id"] for r in db.rows(c, "SELECT id FROM clients WHERE archived=0" + ic + " ORDER BY name", ia)]
        if not ids:
            raise HTTPException(400, "No clients to export")
        firm = archive._safe(db.get_setting(c, "firm_name", "office"))
        return _zip_response(c, ids, f"{firm} - all clients - {engine.today():%Y-%m-%d}.zip", user)


# ============================================================== outbox
@app.post("/api/clients/{cid}/chase")
def chase(cid: int, user: dict = Depends(auth.current)):
    with db.tx() as c:
        auth.require_client(c, user, cid)
        _consent_ok(c, cid)
        draft = engine.build_chase(c, cid)
        if not draft:
            raise HTTPException(400, "Nothing pending from this client in the next 45 days")
        return _queue_message(c, cid, draft["subject"], draft["body"], user)


def _consent_ok(c, cid: int | None) -> None:
    if privacy.withdrawn(c, cid):
        raise HTTPException(400, "This client has withdrawn consent — don't contact them for documents (DPDP s.6(6))")


def _queue_message(c, cid: int, subject: str, body: str, user: dict) -> dict:
    existing = db.row(c, "SELECT id FROM messages WHERE client_id=? AND status='draft' AND direction='out'", (cid,))
    if existing:
        c.execute("UPDATE messages SET body=?, subject=?, created_at=? WHERE id=?", (body, subject, db.now(), existing["id"]))
        mid = existing["id"]
    else:
        mid = c.execute("INSERT INTO messages(client_id,channel,subject,body,status,created_at,direction) "
                        "VALUES(?,?,?,?,'draft',?,'out')", (cid, "whatsapp", subject, body, db.now())).lastrowid
    db.log(c, user["id"], "chase_draft", "client", cid, f"{user['name']} drafted a document request")
    return _message(c, mid)


def _message(c, mid: int) -> dict:
    m = db.row(c, "SELECT m.*, c.name client_name, c.phone, c.email FROM messages m LEFT JOIN clients c ON c.id=m.client_id "
                  "WHERE m.id=?", (mid,))
    from urllib.parse import quote
    phone = m.get("phone") or m.get("from_phone")
    m["whatsapp_url"] = f"https://wa.me/91{phone}?text={quote(m['body'] or '')}" if phone else None
    m["mailto_url"] = f"mailto:{m['email']}?subject={quote(m['subject'] or '')}&body={quote(m['body'] or '')}" if m.get("email") else None
    m["api_send"] = whatsapp.configured() and bool(phone)
    m["window_open"] = bool(phone) and whatsapp.window_open(c, phone)
    return m


@app.get("/api/messages")
def messages(user: dict = Depends(auth.current)):
    with db.tx() as c:
        sc, sa = scope_sql(c, user)
        if sc:
            sc = " AND (client_id IS NULL" + sc.replace(" AND", " OR", 1) + ")"
        return [_message(c, m["id"]) for m in db.rows(c, "SELECT id FROM messages WHERE status!='discarded'" + sc +
                                                         " ORDER BY status IN ('sent','read'), id DESC LIMIT 300", sa)]


class MsgAction(BaseModel):
    action: str
    body: str | None = None


@app.post("/api/messages/{mid}")
def message_action(mid: int, a: MsgAction, user: dict = Depends(auth.current)):
    with db.tx() as c:
        m = db.row(c, "SELECT * FROM messages WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        auth.require_client(c, user, m["client_id"])
        if a.body is not None and m["status"] == "draft":
            c.execute("UPDATE messages SET body=? WHERE id=?", (a.body, mid))
        if a.action == "read":  # an inbound reply has been seen
            c.execute("UPDATE messages SET status='read' WHERE id=? AND direction='in'", (mid,))
        elif a.action in ("sent", "send_whatsapp"):
            auth.require(user, "send_messages")
            if m["direction"] != "out" or m["status"] == "sent":
                raise HTTPException(400, "Already sent")
            if m["subject"] not in ("Personal data breach notice", "Privacy notice"):
                _consent_ok(c, m["client_id"])
            if a.action == "send_whatsapp":
                cl = db.row(c, "SELECT phone FROM clients WHERE id=?", (m["client_id"],)) or {}
                if not cl.get("phone"):
                    raise HTTPException(400, "No WhatsApp number on file for this client")
                try:
                    wa_id = whatsapp.send(c, cl["phone"], a.body if a.body is not None else m["body"])
                except whatsapp.SendError as e:
                    raise HTTPException(400, str(e))
                c.execute("UPDATE messages SET wa_message_id=? WHERE id=?", (wa_id or None, mid))
            c.execute("UPDATE messages SET status='sent', sent_at=?, approved_by=?, sent_by=? WHERE id=?",
                      (db.now(), user["id"], user["id"], mid))
            db.log(c, user["id"], "chase_sent", "client", m["client_id"],
                   f"{user['name']} sent a document request{' on WhatsApp' if a.action == 'send_whatsapp' else ''}")
        elif a.action == "discard":
            c.execute("UPDATE messages SET status='discarded' WHERE id=?", (mid,))
        elif a.action != "save":
            raise HTTPException(400, "Unknown action")
        return _message(c, mid)


# ============================================================ whatsapp
@app.get("/api/whatsapp/webhook", include_in_schema=False)
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and whatsapp.cfg("WA_VERIFY_TOKEN") and \
            q.get("hub.verify_token") == whatsapp.cfg("WA_VERIFY_TOKEN"):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


@app.post("/api/whatsapp/webhook", include_in_schema=False)
async def wa_inbound(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "Bad signature")
    try:
        items = whatsapp.handle_webhook(json.loads(body))
    except ValueError:
        raise HTTPException(400)
    if items:
        background.add_task(whatsapp.process, items)  # answer Meta immediately; download media afterwards
    return {"ok": True}


# ================================================================ brain
class Ask(BaseModel):
    text: str
    history: list[dict[str, Any]] = []


@app.post("/api/ask")
def ask(q: Ask, user: dict = Depends(auth.current)):
    with db.tx() as c:
        return brain.ask(c, q.text, user, q.history)


class Proposal(BaseModel):
    kind: str
    payload: dict[str, Any]


@app.post("/api/proposals/execute")
def execute(p: Proposal, user: dict = Depends(auth.current)):
    """The single door through which the brain's suggestions become real - always human-clicked."""
    with db.tx() as c:
        if p.kind == "add_client":
            cl = _create_client(c, ClientIn(**{k: v for k, v in p.payload.items() if k in ClientIn.model_fields}), user)
            return {"ok": True, "message": f"Added {cl['name']} and scheduled their filings.", "client_id": cl["id"]}
        if p.kind == "override_due":
            o = _propose_override(c, OverrideIn(**p.payload), user)
            if auth.can(user, "approve"):
                c.execute("UPDATE due_overrides SET status='approved', approved_by=? WHERE id=?", (user["id"], o["id"]))
                engine.sync_obligations(c)
                return {"ok": True, "message": "Due date updated across all affected clients."}
            return {"ok": True, "message": "Sent to a partner for approval."}
        if p.kind == "transition":
            pl = p.payload
            _obligation_for(c, int(pl["obligation_id"]), user)
            e = engine.transition(c, int(pl["obligation_id"]), pl["action"], user, pl.get("ack_no"), None, pl.get("note"))
            return {"ok": True, "message": f"{e['form']} {e['period_label']} is now {e['stage'].replace('_', ' ')}."}
        if p.kind == "chase":
            cid = int(p.payload["client_id"])
            auth.require_client(c, user, cid)
            _consent_ok(c, cid)
            draft = engine.build_chase(c, cid)
            body = p.payload.get("body") or (draft and draft["body"])
            if not body:
                raise HTTPException(400, "Nothing to ask this client for")
            _queue_message(c, cid, (draft or {}).get("subject", "Documents needed"), body, user)
            return {"ok": True, "message": "Draft saved to the Outbox — review and send from there."}
        raise HTTPException(400, "Unknown proposal")


@app.get("/api/activity")
def activity(user: dict = Depends(auth.current)):
    with db.tx() as c:
        rows_ = db.rows(c, "SELECT a.*, u.name actor_name FROM activity a LEFT JOIN users u ON u.id=a.actor "
                           "ORDER BY a.id DESC LIMIT 100")
        scope = auth.visible_client_ids(c, user)
        if scope is not None:
            rows_ = [r for r in rows_ if r["actor"] == user["id"] or (r["entity"] == "client" and r["entity_id"] in scope)]
        return rows_


@app.post("/api/demo")
def load_demo(user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    from . import seed
    with db.tx() as c:
        if db.rows(c, "SELECT id FROM clients LIMIT 1"):
            raise HTTPException(400, "Demo data can only be loaded into an empty office")
    logins = seed.run()
    return {"ok": True, "logins": logins}


# ============================================================== privacy
@app.get("/api/public/info")
def public_info(request: Request):
    """Everything the public privacy policy and terms pages show. No login needed, no personal data."""
    with db.tx() as c:
        s = privacy.settings(c)
        return {
            "firm_name": db.get_setting(c, "firm_name", "Your CA Office"),
            "officer_name": s["officer_name"], "officer_email": s["officer_email"], "officer_phone": s["officer_phone"],
            "firm_address": s["firm_address"], "retention_years": s["retention_years"], "response_days": s["response_days"],
            "jurisdiction": s["jurisdiction"], "policy_updated": s["policy_updated"],
            "provider_name": os.environ.get("CABOT_PROVIDER_NAME", "the provider of this software"),
            "shared_with": privacy.shared_with(c), "encrypted": storage.enabled(),
            "purposes": privacy.PURPOSES,
        }


@app.get("/privacy", include_in_schema=False)
def privacy_page():
    return FileResponse(STATIC / "privacy.html")


@app.get("/terms", include_in_schema=False)
def terms_page():
    return FileResponse(STATIC / "terms.html")


def _privacy_admin(user: dict) -> None:
    if not (auth.can(user, "manage_team") or auth.can(user, "approve")):
        raise HTTPException(403, "Only partners or team admins can manage data protection")


class PrivacySettingsIn(BaseModel):
    officer_name: str | None = None
    officer_email: str | None = None
    officer_phone: str | None = None
    firm_address: str | None = None
    retention_years: int | None = None
    response_days: int | None = None
    jurisdiction: str | None = None
    policy_updated: str | None = None
    public_url: str | None = None


@app.post("/api/privacy/settings")
def privacy_settings(body: PrivacySettingsIn, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    with db.tx() as c:
        out = privacy.save_settings(c, body.model_dump())
        db.log(c, user["id"], "privacy_settings", "privacy", None, f"{user['name']} updated the privacy settings")
        return out


@app.get("/api/privacy/overview")
def privacy_overview(user: dict = Depends(auth.current)):
    _privacy_admin(user)
    t = engine.today()
    with db.tx() as c:
        reqs = db.rows(c, "SELECT r.*, c.name client_name FROM dsr r LEFT JOIN clients c ON c.id=r.client_id "
                          "ORDER BY r.status='open' DESC, r.due_at")
        for r in reqs:
            r["overdue"] = r["status"] == "open" and r["due_at"] < t.isoformat()
            r["kind_label"] = privacy.KINDS[r["kind"]]
        return {
            "settings": privacy.settings(c), "kinds": privacy.KINDS, "purposes": privacy.PURPOSES,
            "checklist": privacy.checklist(c, t), "requests": reqs,
            "breaches": [privacy.breach(c, b["id"]) for b in db.rows(c, "SELECT id FROM breaches ORDER BY id DESC")],
            "retention_due": privacy.retention_due(c, t),
        }


class RequestIn(BaseModel):
    kind: str
    client_id: int | None = None
    requester: str = ""
    details: str = ""
    channel: str = ""
    received_at: str | None = None


@app.post("/api/privacy/requests")
def new_request(body: RequestIn, user: dict = Depends(auth.current)):
    with db.tx() as c:
        if body.client_id:
            auth.require_client(c, user, body.client_id)
        try:
            return privacy.create_request(c, body.kind, user, body.client_id, body.requester, body.details, body.channel,
                                          body.received_at)
        except ValueError as e:
            raise HTTPException(400, str(e))


class CloseIn(BaseModel):
    response: str
    status: str = "closed"


@app.post("/api/privacy/requests/{rid}/close")
def close_request(rid: int, body: CloseIn, user: dict = Depends(auth.current)):
    _privacy_admin(user)
    if body.status not in ("closed", "declined"):
        raise HTTPException(400, "Status must be closed or declined")
    with db.tx() as c:
        try:
            return privacy.close_request(c, rid, user, body.response, body.status)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.get("/api/clients/{cid}/privacy")
def client_privacy(cid: int, user: dict = Depends(auth.current)):
    with db.tx() as c:
        auth.require_client(c, user, cid)
        if not db.row(c, "SELECT id FROM clients WHERE id=?", (cid,)):
            raise HTTPException(404)
        return {"summary": privacy.access_summary(c, cid),
                "log": db.rows(c, "SELECT l.*, u.name by_name FROM consent_log l LEFT JOIN users u ON u.id=l.recorded_by "
                                  "WHERE client_id=? ORDER BY l.id DESC", (cid,)),
                "requests": db.rows(c, "SELECT * FROM dsr WHERE client_id=? ORDER BY id DESC", (cid,)),
                "purposes": privacy.PURPOSES}


class ConsentIn(BaseModel):
    action: str
    channel: str = ""
    purposes: list[str] = []
    notes: str = ""
    is_minor: bool | None = None
    guardian_name: str | None = None


@app.post("/api/clients/{cid}/consent")
def client_consent(cid: int, body: ConsentIn, user: dict = Depends(auth.current)):
    with db.tx() as c:
        auth.require_client(c, user, cid)
        try:
            return db.client_dict(privacy.record_consent(c, cid, body.action, user, body.channel, body.purposes, body.notes,
                                                         body.is_minor, body.guardian_name))
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.post("/api/clients/{cid}/privacy-notice")
def client_notice(cid: int, request: Request, user: dict = Depends(auth.current)):
    with db.tx() as c:
        auth.require_client(c, user, cid)
        cl = db.row(c, "SELECT * FROM clients WHERE id=?", (cid,))
        if not cl:
            raise HTTPException(404)
        m = _queue_message(c, cid, "Privacy notice", privacy.notice_message(c, cl, str(request.base_url)), user)
        privacy.record_consent(c, cid, "notice_sent", user, "draft")
        return m


class EraseIn(BaseModel):
    confirm_name: str
    dsr_id: int | None = None


@app.post("/api/clients/{cid}/erase")
def erase(cid: int, body: EraseIn, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    with db.tx() as c:
        cl = db.row(c, "SELECT name FROM clients WHERE id=?", (cid,))
        if not cl:
            raise HTTPException(404)
        if body.confirm_name.strip().lower() != cl["name"].strip().lower():
            raise HTTPException(400, "Type the client's name exactly to confirm erasure")
        return privacy.erase_client(c, cid, user, engine.today(), body.dsr_id)


class PurgeIn(BaseModel):
    ids: list[int]


@app.post("/api/privacy/retention/purge")
def retention_purge(body: PurgeIn, user: dict = Depends(auth.current)):
    auth.require(user, "manage_team")
    with db.tx() as c:
        return {"erased": privacy.purge(c, body.ids, user, engine.today())}


class BreachIn(BaseModel):
    title: str
    description: str = ""
    data_affected: str = ""
    client_ids: list[int] = []
    detected_at: str | None = None


class BreachPatch(BaseModel):
    board_intimated_at: str | None = None
    board_report_at: str | None = None
    principals_notified_at: str | None = None
    measures: str | None = None
    status: str | None = None


@app.post("/api/privacy/breaches")
def new_breach(body: BreachIn, user: dict = Depends(auth.current)):
    _privacy_admin(user)
    with db.tx() as c:
        try:
            return privacy.create_breach(c, user, body.title, body.description, body.data_affected, body.client_ids,
                                         body.detected_at)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.post("/api/privacy/breaches/{bid}")
def update_breach(bid: int, body: BreachPatch, user: dict = Depends(auth.current)):
    _privacy_admin(user)
    with db.tx() as c:
        if not db.row(c, "SELECT id FROM breaches WHERE id=?", (bid,)):
            raise HTTPException(404)
        for k, v in body.model_dump().items():
            if v is not None:
                if k == "status" and v not in ("open", "closed"):
                    raise HTTPException(400, "Status must be open or closed")
                c.execute(f"UPDATE breaches SET {k}=? WHERE id=?", (v, bid))
        db.log(c, user["id"], "breach_update", "privacy", bid, f"{user['name']} updated breach #{bid}")
        return privacy.breach(c, bid)


@app.post("/api/privacy/breaches/{bid}/notify")
def notify_breach(bid: int, user: dict = Depends(auth.current)):
    """Draft a notice to every affected client (s.8(6)). Drafts land in the Outbox for a person to send."""
    _privacy_admin(user)
    with db.tx() as c:
        b = privacy.breach(c, bid)
        n = 0
        for cid in b["client_ids"]:
            cl = db.row(c, "SELECT * FROM clients WHERE id=?", (cid,))
            if cl:
                c.execute("INSERT INTO messages(client_id,channel,subject,body,status,created_at,direction) "
                          "VALUES(?,?,?,?,'draft',?,'out')",
                          (cid, "whatsapp", "Personal data breach notice", privacy.breach_message(c, b, cl), db.now()))
                n += 1
        db.log(c, user["id"], "breach_notify", "privacy", bid, f"{user['name']} drafted {n} breach notices")
        return {"drafted": n}


# ================================================================ static
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
