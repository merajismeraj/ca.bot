"""Orchestration: ingest -> classify -> link to obligations -> readiness -> next action.

The office dumps files once. From then on the engine keeps three things true:
  1. every client has a materialised compliance calendar for the live FYs,
  2. every obligation knows which documents it still needs,
  3. every hand-off (classification doubt, partner approval, client chase,
     due-date extension) waits for a human before anything leaves the office.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

from . import classifier, db
from . import fy as F
from .catalog import DOC_TYPES
from .compliance import RULES_BY_CODE, generate, required_months

ACCEPTED_SUFFIXES = {".pdf", ".csv", ".xlsx", ".xlsm", ".xls", ".txt", ".json", ".xml", ".jpg", ".jpeg", ".png", ".tsv"}
DONE = ("filed", "na", "not_tracked")


class WorkflowError(ValueError):
    pass


def today() -> date:
    v = os.environ.get("CABOT_TODAY")
    return date.fromisoformat(v) if v else date.today()


def live_fys() -> list[int]:
    cur = F.fy_of(today())
    return [cur - 1, cur]


# ----------------------------------------------------------- obligations
def approved_overrides(conn) -> dict[tuple[str, int, str], str]:
    out = {}
    for o in db.rows(conn, "SELECT * FROM due_overrides WHERE status='approved' ORDER BY id"):
        out[(o["rule_code"], o["fy"], o["period_key"])] = o["new_due"]
    return out


def sync_obligations(conn, client_id: int | None = None) -> int:
    """Materialise / refresh the calendar. Idempotent; preserves work status."""
    tracking_start = db.get_setting(conn, "tracking_start", today().isoformat())
    overrides = approved_overrides(conn)
    q = "SELECT * FROM clients WHERE archived=0" + (" AND id=?" if client_id else "")
    clients = [db.client_dict(c) for c in db.rows(conn, q, (client_id,) if client_id else ())]
    created = 0
    for c in clients:
        keep = set()
        for fy in live_fys():
            for rule, inst in generate(c, fy):
                due = overrides.get((rule.code, fy, inst.period_key)) or overrides.get((rule.code, fy, "*")) \
                    or inst.due.isoformat()
                keep.add((rule.code, fy, inst.period_key))
                existing = db.row(conn, "SELECT id, status FROM obligations WHERE client_id=? AND rule_code=? "
                                        "AND fy=? AND period_key=?", (c["id"], rule.code, fy, inst.period_key))
                if existing:
                    conn.execute("UPDATE obligations SET due=?, due_default=?, months=? WHERE id=?",
                                 (due, inst.due.isoformat(), json.dumps(inst.months), existing["id"]))
                    continue
                status = "not_tracked" if due < tracking_start else "open"
                conn.execute(
                    "INSERT INTO obligations(client_id,rule_code,fy,period_key,months,due_default,due,status,"
                    "assignee_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], rule.code, fy, inst.period_key, json.dumps(inst.months), inst.due.isoformat(), due,
                     status, c.get("assignee_id"), db.now()))
                created += 1
        # profile changed (e.g. GST scheme switched): drop untouched obligations that no longer apply
        for o in db.rows(conn, "SELECT id, rule_code, fy, period_key FROM obligations WHERE client_id=? "
                               "AND status IN ('open','not_tracked')", (c["id"],)):
            if (o["rule_code"], o["fy"], o["period_key"]) not in keep and o["fy"] in live_fys():
                conn.execute("DELETE FROM obligations WHERE id=?", (o["id"],))
    return created


def coverage(conn, client_id: int) -> dict[str, set[str]]:
    cov: dict[str, set[str]] = defaultdict(set)
    for d in db.rows(conn, "SELECT doc_type, period_start, period_end FROM documents WHERE client_id=? "
                           "AND status IN ('auto','confirmed') AND period_start IS NOT NULL", (client_id,)):
        cov[d["doc_type"]].update(F.months_between(d["period_start"], d["period_end"] or d["period_start"]))
    return cov


def enrich(conn, obls: list[dict]) -> list[dict]:
    """Attach readiness, missing docs, blockers and the single next action."""
    cov_cache: dict[int, dict] = {}
    clients = {c["id"]: db.client_dict(c) for c in db.rows(conn, "SELECT * FROM clients")}
    users = {u["id"]: u for u in db.rows(conn, "SELECT * FROM users")}
    t = today()
    out = []
    for o in obls:
        o = dict(o)
        client = clients[o["client_id"]]
        rule = RULES_BY_CODE.get(o["rule_code"])
        if not rule:
            continue
        cov = cov_cache.setdefault(o["client_id"], coverage(conn, o["client_id"]))
        months = tuple(json.loads(o["months"]))
        inst = type("I", (), {"months": months})
        need, have, missing = 0, 0.0, []
        for req in rule.reqs(client):
            rm = required_months(req, inst, o["fy"])
            got = [m for m in rm if m in cov.get(req.doc_type, ())]
            need += 1
            have += len(got) / len(rm)
            if len(got) < len(rm):
                gaps = [m for m in rm if m not in got]
                missing.append({"doc_type": req.doc_type, "label": DOC_TYPES[req.doc_type]["label"],
                                "months": gaps, "period": F.period_label(gaps, o["fy"]) if len(gaps) > 1 else F.month_label(gaps[0])})
        blockers = []
        if rule.depends_on:
            ph = ",".join("?" * len(rule.depends_on))
            for b in db.rows(conn, f"SELECT rule_code, period_key FROM obligations WHERE client_id=? AND fy=? "
                                   f"AND rule_code IN ({ph}) AND status NOT IN ('filed','na','not_tracked')",
                             (o["client_id"], o["fy"], *rule.depends_on)):
                blockers.append(f"{RULES_BY_CODE[b['rule_code']].form} {b['period_key']}")
        due = date.fromisoformat(o["due"])
        o.update({
            "client_name": client["name"],
            "rule_name": rule.name, "form": rule.form, "authority": rule.authority,
            "help": rule.help, "penalty": rule.penalty,
            "period_label": period_title(rule.code, o["period_key"], list(months), o["fy"]),
            "fy_label": F.fy_label(o["fy"]),
            "months": list(months),
            "readiness": 100 if need == 0 else round(100 * have / need),
            "missing": missing,
            "blockers": blockers,
            "days_left": (due - t).days,
            "due_shifted": o["due"] != o["due_default"],
            "assignee": users.get(o["assignee_id"], {}).get("name"),
        })
        o["stage"] = stage(o)
        o["next_action"] = next_action(o)
        out.append(o)
    return out


def period_title(code: str, period_key: str, months: list[str], fy: int) -> str:
    if code == "ITR":
        return f"{F.fy_label(fy)} ({F.ay_label(fy)})"
    if period_key.startswith("I"):
        return f"Instalment {period_key[1]} · {F.fy_label(fy)}"
    return F.period_label(months, fy)


def stage(o: dict) -> str:
    if o["status"] != "open":
        return o["status"]
    t = today()
    if o["months"][-1] >= F.month_key(t.year, t.month):
        return "upcoming"  # the period is still running: nothing to ask the client for yet
    if o["missing"]:
        return "awaiting_docs"
    return "blocked" if o["blockers"] else "ready"


def next_action(o: dict) -> dict | None:
    s = o["stage"]
    return {
        "awaiting_docs": {"key": "chase", "label": "Ask client for documents", "role": "staff"},
        "blocked": None,
        "ready": {"key": "start", "label": "Start preparing", "role": "staff"},
        "in_progress": {"key": "submit", "label": "Send for partner review", "role": "staff"},
        "review": {"key": "approve", "label": "Approve for filing", "role": "partner"},
        "approved": {"key": "file", "label": "Record filing (ack no.)", "role": "staff"},
    }.get(s)


TRANSITIONS = {
    # action: (allowed from, to, partner_only)
    "start": (("open",), "in_progress", False),
    "submit": (("in_progress",), "review", False),
    "send_back": (("review", "approved"), "in_progress", True),
    "approve": (("review",), "approved", True),
    "file": (("approved",), "filed", False),
    "mark_na": (("open", "in_progress", "review", "not_tracked"), "na", True),
    "reopen": (("filed", "na", "not_tracked"), "open", True),
}


def transition(conn, obl_id: int, action: str, user: dict, ack_no: str | None = None,
               filed_on: str | None = None, note: str | None = None) -> dict:
    o = db.row(conn, "SELECT * FROM obligations WHERE id=?", (obl_id,))
    if not o:
        raise WorkflowError("Obligation not found")
    if action not in TRANSITIONS:
        raise WorkflowError(f"Unknown action {action}")
    allowed, to, partner_only = TRANSITIONS[action]
    if o["status"] not in allowed:
        raise WorkflowError(f"Can't {action.replace('_', ' ')} from '{o['status']}'")
    if partner_only and user["role"] != "partner":
        raise WorkflowError("Only a partner can do this")
    e = enrich(conn, [o])[0]
    if action == "submit" and e["blockers"]:
        raise WorkflowError("File these first: " + ", ".join(e["blockers"]))
    if action == "file":
        if not (ack_no and ack_no.strip()):
            raise WorkflowError("Acknowledgement / ARN number is required to mark as filed")
        filed_on = filed_on or today().isoformat()
    if action in ("mark_na", "reopen", "send_back") and not (note and note.strip()):
        raise WorkflowError("Please add a reason")
    conn.execute("UPDATE obligations SET status=?, ack_no=COALESCE(?,ack_no), filed_on=COALESCE(?,filed_on), "
                 "notes=COALESCE(?,notes), updated_at=? WHERE id=?",
                 (to, ack_no, filed_on, note, db.now(), obl_id))
    label = f"{e['form']} {e['period_label']} · {e['client_name']}"
    extra = f" (ack {ack_no})" if ack_no else (f" — {note}" if note else "")
    db.log(conn, user["id"], action, "obligation", obl_id, f"{user['name']}: {action.replace('_', ' ')} {label}{extra}")
    return enrich(conn, [db.row(conn, "SELECT * FROM obligations WHERE id=?", (obl_id,))])[0]


# ------------------------------------------------------------- documents
def _learned(conn) -> tuple[dict[str, int], dict[str, str]]:
    lc, lt = {}, {}
    for r in db.rows(conn, "SELECT kind, token, value FROM learned"):
        if r["kind"] == "client":
            lc[r["token"]] = int(r["value"])
        else:
            lt[r["token"]] = r["value"]
    return lc, lt


def expand_upload(filename: str, data: bytes) -> list[tuple[str, bytes]]:
    """Unpack zips (one level of nesting) so a whole office folder can be dropped at once."""
    if not filename.lower().endswith(".zip"):
        return [(filename, data)]
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            p = PurePosixPath(info.filename)
            if info.is_dir() or any(part.startswith(("__MACOSX", ".")) for part in p.parts):
                continue
            if info.file_size > 50 * 1024 * 1024:
                continue
            inner = z.read(info)
            rel = f"{Path(filename).name}/{info.filename}"
            if p.suffix.lower() == ".zip":
                out.extend((f"{Path(filename).name}/{n}", b) for n, b in expand_upload(p.name, inner))
            else:
                out.append((rel, inner))
    return out


def ingest(conn, rel_path: str, data: bytes, user: dict, batch_id: str, hint_client_id: int | None = None) -> dict:
    sha = hashlib.sha256(data).hexdigest()
    dup = db.row(conn, "SELECT * FROM documents WHERE sha256=?", (sha,))
    if dup:
        return {**dup, "duplicate": True}
    suffix = Path(rel_path).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        return {"filename": rel_path, "skipped": True, "reason": f"Unsupported file type {suffix or '(none)'}"}
    stored = db.UPLOAD_DIR / f"{sha[:2]}/{sha}{suffix}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
    text = classifier.extract_text(stored)
    clients = [db.client_dict(c) for c in db.rows(conn, "SELECT * FROM clients WHERE archived=0")]
    lc, lt = _learned(conn)
    r = classifier.classify(rel_path, text, clients, lc, lt, hint_client_id, today())
    cur = conn.execute(
        "INSERT INTO documents(sha256,filename,stored_path,size,text_excerpt,client_id,doc_type,fy,period_start,"
        "period_end,confidence,confs,status,reasons,batch_id,uploaded_by,uploaded_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha, rel_path, str(stored), len(data), text[:1500], r.client_id, r.doc_type, r.fy, r.period_start,
         r.period_end, r.confidence, json.dumps({"client": r.client_conf, "type": r.type_conf, "period": r.period_conf}),
         r.status, json.dumps(r.reasons), batch_id, user["id"], db.now()))
    return db.row(conn, "SELECT * FROM documents WHERE id=?", (cur.lastrowid,))


def folder_consensus(conn, batch_id: str) -> int:
    """Files in one folder usually belong to one client: let confident siblings vouch for the rest."""
    docs = db.rows(conn, "SELECT * FROM documents WHERE batch_id=? AND status IN ('auto','needs_review')", (batch_id,))
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        parts = PurePosixPath(d["filename"]).parts
        if len(parts) > 1:
            groups["/".join(parts[:-1])].append(d)
            if len(parts) > 2:
                groups[parts[0]].append(d)
    fixed = 0
    for folder, members in sorted(groups.items(), key=lambda kv: -kv[0].count("/")):
        if folder.lower().endswith(".zip"):
            continue  # a zip is a bag of files, not evidence that they share a client
        sure = {d["client_id"] for d in members if json.loads(d["confs"] or "{}").get("client", 0) >= 0.9}
        if len(sure) != 1 or sum(1 for d in members if d["client_id"] in sure) < 2:
            continue
        cid = sure.pop()
        name = db.row(conn, "SELECT name FROM clients WHERE id=?", (cid,))["name"]
        for d in members:
            cur = db.row(conn, "SELECT * FROM documents WHERE id=?", (d["id"],))
            confs = json.loads(cur["confs"] or "{}")
            if confs.get("client", 0) >= 0.8 or cur["status"] == "confirmed":
                continue
            confs["client"] = 0.9
            overall = round(min(confs.values()), 2)
            reasons = [f"Client: other files in “{folder}” belong to {name}"] + \
                      [r for r in json.loads(cur["reasons"] or "[]") if not r.startswith("Client:")]
            conn.execute("UPDATE documents SET client_id=?, confs=?, confidence=?, status=?, reasons=? WHERE id=?",
                         (cid, json.dumps(confs), overall,
                          "auto" if overall >= classifier.AUTO_THRESHOLD else "needs_review", json.dumps(reasons), d["id"]))
            fixed += 1
    return fixed


def confirm_document(conn, doc_id: int, user: dict, client_id: int | None, doc_type: str | None,
                     period_start: str | None, period_end: str | None) -> dict:
    d = db.row(conn, "SELECT * FROM documents WHERE id=?", (doc_id,))
    if not d:
        raise WorkflowError("Document not found")
    client_id = client_id or d["client_id"]
    doc_type = doc_type or d["doc_type"]
    period_start = period_start or d["period_start"]
    period_end = period_end or period_start
    if not (client_id and doc_type and period_start):
        raise WorkflowError("Client, document type and period are all needed")
    if doc_type not in DOC_TYPES:
        raise WorkflowError("Unknown document type")
    if period_end < period_start:
        raise WorkflowError("Period end is before period start")
    y, m = F.parse_month_key(period_start)
    # learn from the correction so the next file like this is filed automatically
    names = [c["name"] for c in db.rows(conn, "SELECT name FROM clients")]
    toks = classifier.learnable_tokens(d["filename"], names)
    if client_id != d["client_id"] or json.loads(d["confs"] or "{}").get("client", 0) < 0.9:
        for t in toks:
            conn.execute("INSERT INTO learned(kind,token,value,created_at) VALUES('client',?,?,?) "
                         "ON CONFLICT(kind,token) DO UPDATE SET value=excluded.value", (t, str(client_id), db.now()))
    if doc_type != d["doc_type"]:
        for t in toks[:1]:
            conn.execute("INSERT INTO learned(kind,token,value,created_at) VALUES('type',?,?,?) "
                         "ON CONFLICT(kind,token) DO UPDATE SET value=excluded.value", (t, doc_type, db.now()))
    conn.execute("UPDATE documents SET client_id=?, doc_type=?, period_start=?, period_end=?, fy=?, status='confirmed', "
                 "confidence=1.0 WHERE id=?", (client_id, doc_type, period_start, period_end, F.fy_of(date(y, m, 1)), doc_id))
    db.log(conn, user["id"], "doc_confirm", "document", doc_id, f"{user['name']} filed {d['filename']}")
    return db.row(conn, "SELECT * FROM documents WHERE id=?", (doc_id,))


# ---------------------------------------------------------------- chase
def build_chase(conn, client_id: int, horizon_days: int = 45) -> dict | None:
    """Draft a single, polite, consolidated document request for a client."""
    client = db.client_dict(db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,)))
    t = today()
    obls = enrich(conn, db.rows(conn, "SELECT * FROM obligations WHERE client_id=? AND status='open' AND due<=? "
                                      "ORDER BY due", (client_id, (t + timedelta(days=horizon_days)).isoformat())))
    wanted: dict[str, dict] = {}
    earliest: dict[str, str] = {}
    this_month = F.month_key(t.year, t.month)
    for o in obls:
        for m in o["missing"]:
            closed = [mk for mk in m["months"] if mk < this_month]  # never ask for a month that hasn't ended
            if not closed:
                continue
            w = wanted.setdefault(m["doc_type"], {"label": m["label"], "months": set()})
            w["months"].update(closed)
            earliest[m["doc_type"]] = min(earliest.get(m["doc_type"], o["due"]), o["due"])
    if not wanted:
        return None
    lines = []
    for dt, w in sorted(wanted.items(), key=lambda kv: earliest[kv[0]]):
        months = sorted(w["months"])
        y, m = F.parse_month_key(months[0])
        span = F.period_label(months, F.fy_of(date(y, m, 1)))
        need_by = date.fromisoformat(earliest[dt]) - timedelta(days=5)
        when = "urgent – please send today" if need_by <= t else f"by {need_by.strftime('%d %b')}"
        lines.append(f"• {w['label']} — {span} ({when})")
    firm = db.get_setting(conn, "firm_name", "our office")
    greet = client.get("contact_name") or client["name"]
    body = (f"Namaste {greet},\n\nTo complete your upcoming filings on time, please share:\n"
            + "\n".join(lines)
            + "\n\nYou can reply on WhatsApp or email the files — PDFs, Excel or phone photos are all fine.\n\n"
            + f"Thank you,\n{firm}")
    return {"client": client, "body": body, "items": len(lines), "subject": f"Documents needed – {client['name']}"}


def doc_summary(d: dict) -> dict:
    d = dict(d)
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["type_label"] = DOC_TYPES.get(d["doc_type"] or "other", DOC_TYPES["other"])["label"]
    if d.get("period_start"):
        ms = F.months_between(d["period_start"], d["period_end"] or d["period_start"])
        d["period_label"] = F.period_label(ms, d["fy"]) if d.get("fy") is not None else F.month_label(ms[0])
    else:
        d["period_label"] = None
    d.pop("text_excerpt", None)
    d.pop("confs", None)
    d.pop("stored_path", None)
    return d
