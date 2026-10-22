"""Digital Personal Data Protection Act, 2023 (DPDP) controls.

The CA office using this software is the Data Fiduciary for its clients'
personal data. These functions give it the records and workflows the Act and
the DPDP Rules, 2025 expect - they don't make anyone compliant on their own:

  notice & consent (s.5, s.6)      consent log per client, notice drafts, withdrawal
  children (s.9)                   minor flag + guardian on record before consent counts
  data principal rights (s.11-14)  request register with response deadlines
  security & breach (s.8(5),(6))   encryption at rest, access logs, breach register with the 72-hour clock
  retention & erasure (s.8(7))     erase when the purpose is served, except what law requires to be kept
  grievance contact (s.8(9))       published in the privacy policy

Erasure and purges always need a person with the Team permission to click.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, storage

DEFAULTS = {
    "officer_name": "", "officer_email": "", "officer_phone": "", "firm_address": "",
    "retention_years": 8,      # Companies Act s.128 (8 FY); CGST s.36 (72 months); IT records; ICAI SQC 1 (7 yrs)
    "response_days": 30,       # DPDP Rules cap grievance responses at 90 days; offices usually promise less
    "jurisdiction": "", "policy_updated": "", "public_url": "",
}
KINDS = {
    "access": "Summary of personal data and processing (s.11)",
    "correction": "Correct, complete or update data (s.12)",
    "erasure": "Erase personal data (s.12)",
    "withdraw": "Withdraw consent (s.6(4))",
    "grievance": "Grievance (s.13)",
    "nomination": "Nominate a person (s.14)",
}
PURPOSES = ["Accounting and book-keeping", "Tax and GST return filing", "Statutory compliance and ROC filings",
            "Audit and assurance", "Communication about your engagement"]


def settings(conn) -> dict:
    return DEFAULTS | (db.get_setting(conn, "privacy", {}) or {})


def save_settings(conn, values: dict) -> dict:
    cur = settings(conn)
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            cur[k] = values[k]
    cur["retention_years"] = max(1, int(cur["retention_years"]))
    cur["response_days"] = min(90, max(1, int(cur["response_days"])))
    db.set_setting(conn, "privacy", cur)
    return cur


# ---------------------------------------------------------------- consent
def record_consent(conn, client_id: int, action: str, user: dict, channel: str = "", purposes: list[str] | None = None,
                   notes: str = "", is_minor: bool | None = None, guardian_name: str | None = None) -> dict:
    c = db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,))
    if not c:
        raise ValueError("Client not found")
    if action not in ("notice_sent", "given", "withdrawn"):
        raise ValueError("Unknown consent action")
    if is_minor is not None:
        conn.execute("UPDATE clients SET is_minor=?, guardian_name=? WHERE id=?",
                     (int(is_minor), (guardian_name or "").strip() or None, client_id))
        c = db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,))
    if action == "given" and c["is_minor"] and not c["guardian_name"]:
        raise ValueError("This client is a minor: record the parent/guardian giving consent (DPDP s.9)")
    now = db.now()
    if action == "notice_sent":
        conn.execute("UPDATE clients SET notice_at=? WHERE id=?", (now, client_id))
    else:
        conn.execute("UPDATE clients SET consent_status=?, consent_at=? WHERE id=?", (action, now, client_id))
    conn.execute("INSERT INTO consent_log(client_id,action,channel,purposes,notes,recorded_by,at) VALUES(?,?,?,?,?,?,?)",
                 (client_id, action, channel, json.dumps(purposes or []), notes, user["id"], now))
    label = {"notice_sent": "sent the privacy notice to", "given": "recorded consent from", "withdrawn": "recorded consent withdrawal by"}
    who = f"{c['name']} (guardian {c['guardian_name']})" if c["is_minor"] and c["guardian_name"] else c["name"]
    db.log(conn, user["id"], f"consent_{action}", "client", client_id, f"{user['name']} {label[action]} {who}")
    return db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,))


def withdrawn(conn, client_id: int | None) -> bool:
    if not client_id:
        return False
    r = db.row(conn, "SELECT consent_status FROM clients WHERE id=?", (client_id,))
    return bool(r and r["consent_status"] == "withdrawn")


def notice_message(conn, client: dict, base_url: str) -> str:
    s = settings(conn)
    firm = db.get_setting(conn, "firm_name", "our office")
    url = (s["public_url"] or base_url).rstrip("/") + "/privacy"
    greet = client.get("guardian_name") if client.get("is_minor") else (client.get("contact_name") or client["name"])
    lines = [f"Namaste {greet},", "",
             f"{firm} processes your personal and financial information (identity details, bank statements, invoices, "
             "tax and payroll records) only to provide accounting, tax-filing and compliance services you engage us for.",
             "", f"Our privacy notice explains what we hold, why, how long we keep it and your rights under the Digital "
             f"Personal Data Protection Act, 2023: {url}", ""]
    if s["officer_email"] or s["officer_phone"]:
        lines.append(f"Questions or requests: {s['officer_name'] or 'our grievance officer'}, "
                     f"{' / '.join(x for x in (s['officer_email'], s['officer_phone']) if x)}")
        lines.append("")
    lines += ["Please reply “I agree” to confirm your consent. You can withdraw it at any time by telling us.",
              "", f"Thank you,\n{firm}"]
    return "\n".join(lines)


# ---------------------------------------------------------- retention
def hold_until(doc: dict, years: int) -> date:
    """Earliest date a document may be erased: end of its FY + the retention period required by law."""
    if doc.get("fy") is not None:
        return date(doc["fy"] + 1 + years, 3, 31)
    up = (doc.get("uploaded_at") or db.now())[:10]
    return date.fromisoformat(up).replace(year=int(up[:4]) + years)


def retention_due(conn, today: date) -> list[dict]:
    years = settings(conn)["retention_years"]
    out = []
    for d in db.rows(conn, "SELECT d.id, d.filename, d.fy, d.uploaded_at, d.client_id, d.stored_path, c.name client_name "
                           "FROM documents d LEFT JOIN clients c ON c.id=d.client_id WHERE d.stored_path IS NOT NULL"):
        h = hold_until(d, years)
        if h < today:
            out.append({"id": d["id"], "filename": Path(d["filename"]).name, "client_name": d["client_name"],
                        "fy": d["fy"], "hold_until": h.isoformat()})
    return out


def _erase_doc(conn, d: dict) -> None:
    storage.delete(d["stored_path"])
    conn.execute("UPDATE documents SET stored_path=NULL, text_excerpt=NULL, filename=?, status='erased', reasons='[]' "
                 "WHERE id=?", (f"[erased] {Path(d['filename']).suffix}", d["id"]))


def purge(conn, doc_ids: list[int], user: dict, today: date) -> int:
    due = {d["id"] for d in retention_due(conn, today)}
    n = 0
    for did in doc_ids:
        if did not in due:
            continue  # still under legal hold - never erased from here
        _erase_doc(conn, db.row(conn, "SELECT * FROM documents WHERE id=?", (did,)))
        n += 1
    db.log(conn, user["id"], "retention_purge", "privacy", None, f"{user['name']} erased {n} files past their retention period")
    return n


def erase_client(conn, client_id: int, user: dict, today: date, dsr_id: int | None = None) -> dict:
    """Erase a client's personal data except what tax / company / GST law requires the office to keep."""
    c = db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,))
    if not c:
        raise ValueError("Client not found")
    years = settings(conn)["retention_years"]
    erased, held, latest_hold = 0, 0, None
    for d in db.rows(conn, "SELECT * FROM documents WHERE client_id=? AND stored_path IS NOT NULL", (client_id,)):
        h = hold_until(d, years)
        if h < today:
            _erase_doc(conn, d)
            erased += 1
        else:
            held += 1
            latest_hold = max(latest_hold or h, h)
    conn.execute("DELETE FROM messages WHERE client_id=?", (client_id,))
    conn.execute("DELETE FROM obligations WHERE client_id=? AND status IN ('open','not_tracked')", (client_id,))
    if held:
        # records the law requires us to keep: restrict, strip contact details, stop all further processing
        conn.execute("UPDATE clients SET phone=NULL, email=NULL, contact_name=NULL, aliases='[]', archived=1, "
                     "consent_status='withdrawn' WHERE id=?", (client_id,))
        result = f"Erased {erased} files and all messages. {held} files are kept under legal retention until " \
                 f"{latest_hold:%d %b %Y}, then become erasable. Client archived and contact details removed."
    else:
        conn.execute("UPDATE clients SET name=?, pan=NULL, gstin=NULL, tan=NULL, phone=NULL, email=NULL, contact_name=NULL, "
                     "guardian_name=NULL, aliases='[]', archived=1, consent_status='withdrawn', erased_at=? WHERE id=?",
                     (f"Erased client #{client_id}", db.now(), client_id))
        conn.execute("UPDATE documents SET client_id=NULL WHERE client_id=?", (client_id,))
        result = f"Erased {erased} files, all messages and the client profile."
    db.log(conn, user["id"], "erasure", "client", client_id, f"{user['name']}: {result}")
    if dsr_id:
        close_request(conn, dsr_id, user, result)
    return {"erased_files": erased, "held_files": held, "held_until": latest_hold.isoformat() if latest_hold else None,
            "message": result}


def access_summary(conn, client_id: int) -> dict:
    """What a data principal is entitled to under s.11: what we hold, why, and who it was shared with."""
    c = db.client_dict(db.row(conn, "SELECT * FROM clients WHERE id=?", (client_id,)))
    docs = db.rows(conn, "SELECT doc_type, COUNT(*) n, MIN(period_start) first, MAX(period_end) last FROM documents "
                         "WHERE client_id=? AND stored_path IS NOT NULL GROUP BY doc_type", (client_id,))
    from .catalog import DOC_TYPES
    return {
        "profile": {k: c[k] for k in ("name", "pan", "gstin", "tan", "entity_type", "contact_name", "phone", "email",
                                      "is_minor", "guardian_name")},
        "documents": [{"type": DOC_TYPES.get(d["doc_type"] or "other", DOC_TYPES["other"])["label"], "count": d["n"],
                       "covering": f"{d['first'] or '?'} to {d['last'] or '?'}"} for d in docs],
        "purposes": PURPOSES,
        "consent": db.rows(conn, "SELECT action, channel, at FROM consent_log WHERE client_id=? ORDER BY id", (client_id,)),
        "shared_with": shared_with(conn),
        "generated_at": db.now(),
    }


def shared_with(conn) -> list[str]:
    from . import brain, ocr, whatsapp
    out = ["Government portals (GSTN, Income Tax, MCA, TRACES, EPFO/ESIC) when filings are made on your behalf"]
    if brain.llm_available():
        out.append("Anthropic PBC (USA) — AI assistant: client names, filing statuses and due dates")
    if ocr.vision_enabled():
        out.append("Anthropic PBC (USA) — reading of scanned documents that local OCR cannot read")
    from . import google_auth
    if google_auth.configured():
        out.append("Google LLC — staff sign-in (name and work email of our team members only)")
    if whatsapp.configured():
        out.append("Meta Platforms (WhatsApp Business) — messages and files you exchange with us on WhatsApp")
    return out


# ------------------------------------------------------- rights requests
def create_request(conn, kind: str, user: dict, client_id: int | None, requester: str, details: str, channel: str,
                   received_at: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown request type")
    if not (client_id or requester.strip()):
        raise ValueError("Say who made the request")
    rec = received_at or date.today().isoformat()
    due = (date.fromisoformat(rec[:10]) + timedelta(days=settings(conn)["response_days"])).isoformat()
    rid = conn.execute("INSERT INTO dsr(client_id,requester,kind,details,channel,received_at,due_at,status,handled_by) "
                       "VALUES(?,?,?,?,?,?,?,'open',?)", (client_id, requester.strip(), kind, details, channel, rec, due,
                                                          user["id"])).lastrowid
    if kind == "withdraw" and client_id:
        record_consent(conn, client_id, "withdrawn", user, channel, notes=f"Request #{rid}")
    db.log(conn, user["id"], "dsr_open", "privacy", rid, f"{user['name']} logged a {kind} request (respond by {due})")
    return db.row(conn, "SELECT * FROM dsr WHERE id=?", (rid,))


def close_request(conn, rid: int, user: dict, response: str, status: str = "closed") -> dict:
    if not (response or "").strip():
        raise ValueError("Record what was done or why the request was declined")
    conn.execute("UPDATE dsr SET status=?, response=?, closed_at=?, handled_by=? WHERE id=?",
                 (status, response.strip(), db.now(), user["id"], rid))
    db.log(conn, user["id"], "dsr_close", "privacy", rid, f"{user['name']} {status} request #{rid}")
    return db.row(conn, "SELECT * FROM dsr WHERE id=?", (rid,))


# --------------------------------------------------------------- breaches
def create_breach(conn, user: dict, title: str, description: str, data_affected: str, client_ids: list[int],
                  detected_at: str | None = None) -> dict:
    if not title.strip():
        raise ValueError("Give the incident a short title")
    bid = conn.execute("INSERT INTO breaches(title,description,data_affected,client_ids,detected_at,recorded_by) "
                       "VALUES(?,?,?,?,?,?)", (title.strip(), description, data_affected, json.dumps(client_ids),
                                               detected_at or db.now(), user["id"])).lastrowid
    db.log(conn, user["id"], "breach_open", "privacy", bid, f"{user['name']} recorded a personal data breach: {title}")
    return breach(conn, bid)


def breach(conn, bid: int) -> dict:
    b = db.row(conn, "SELECT * FROM breaches WHERE id=?", (bid,))
    b["client_ids"] = json.loads(b["client_ids"] or "[]")
    detected = datetime.fromisoformat(b["detected_at"])
    b["report_due"] = (detected + timedelta(hours=72)).isoformat(timespec="minutes")
    b["hours_left"] = round(((detected + timedelta(hours=72)) - datetime.now()).total_seconds() / 3600, 1)
    return b


def breach_message(conn, b: dict, client: dict) -> str:
    s = settings(conn)
    firm = db.get_setting(conn, "firm_name", "our office")
    contact = " / ".join(x for x in (s["officer_email"], s["officer_phone"]) if x) or "this number"
    return (f"Dear {client.get('contact_name') or client['name']},\n\nWe are writing to inform you of a personal data breach "
            f"at {firm} that may involve your information.\n\nWhat happened: {b['description'] or b['title']}\n"
            f"Information involved: {b['data_affected'] or 'being assessed'}\nWhen we found it: {b['detected_at'][:16].replace('T', ' ')}\n"
            f"What we have done: {b['measures'] or 'contained the incident and are investigating'}\n\n"
            "What you can do: watch for unexpected OTPs, calls or emails asking for bank or tax details; do not share OTPs; "
            "consider changing passwords for your income-tax / GST portal logins.\n\n"
            f"For questions, contact {s['officer_name'] or 'our grievance officer'} at {contact}.\n\n{firm}")


# --------------------------------------------------------------- checklist
def checklist(conn, today: date) -> list[dict]:
    from . import auth
    s = settings(conn)
    clients = db.rows(conn, "SELECT consent_status, is_minor, guardian_name FROM clients WHERE archived=0")
    n = len(clients)
    consented = sum(1 for c in clients if c["consent_status"] == "given")
    minors_missing = sum(1 for c in clients if c["is_minor"] and not c["guardian_name"])
    overdue = db.row(conn, "SELECT COUNT(*) n FROM dsr WHERE status='open' AND due_at<?", (today.isoformat(),))["n"]
    open_breaches = [breach(conn, b["id"]) for b in db.rows(conn, "SELECT id FROM breaches WHERE status='open'")]
    late_breach = [b for b in open_breaches if not b["board_report_at"]]
    due = len(retention_due(conn, today))
    import os
    return [
        {"key": "officer", "ok": bool(s["officer_name"] and (s["officer_email"] or s["officer_phone"])),
         "title": "Grievance officer published", "law": "s.8(9), s.13",
         "detail": "Name and contact of the person who answers data protection questions appear in the privacy policy."},
        {"key": "address", "ok": bool(s["firm_address"]), "title": "Office address in the privacy notice", "law": "s.5",
         "detail": "Clients must know who the Data Fiduciary is."},
        {"key": "consent", "ok": n == 0 or consented == n, "title": f"Consent on record ({consented}/{n} clients)",
         "law": "s.5, s.6", "detail": "Send the privacy notice and record each client's consent from their profile."},
        {"key": "minors", "ok": minors_missing == 0, "title": "Guardian consent for minor clients", "law": "s.9",
         "detail": f"{minors_missing} minor client(s) without a guardian on record." if minors_missing else "No gaps."},
        {"key": "encryption", "ok": storage.enabled(), "title": "Client files encrypted at rest", "law": "s.8(5), Rules",
         "detail": "Set CABOT_DATA_KEY (python -m app.storage new-key) and run encrypt-all; also use an encrypted disk."},
        {"key": "https", "ok": auth.secure_cookies(), "title": "Served over HTTPS only", "law": "s.8(5)",
         "detail": "Put the app behind HTTPS and set CABOT_SECURE_COOKIES=1."},
        {"key": "requests", "ok": overdue == 0, "title": "Rights requests answered on time", "law": "s.11-13",
         "detail": f"{overdue} request(s) past their response date." if overdue else "None overdue."},
        {"key": "breach", "ok": not late_breach, "title": "Breaches reported to the Data Protection Board", "law": "s.8(6), Rules",
         "detail": "Intimate the Board and affected clients without delay; detailed report within 72 hours."
         if late_breach else "No open incidents awaiting a report."},
        {"key": "retention", "ok": due == 0, "title": "No data kept beyond its retention period", "law": "s.8(7)",
         "detail": f"{due} file(s) are past the {s['retention_years']}-year retention period and can be erased." if due
         else f"Retention period: {s['retention_years']} years after the end of the financial year."},
        {"key": "backups", "ok": bool(os.environ.get("CABOT_BACKUPS_CONFIRMED")), "title": "Backups encrypted and tested",
         "law": "s.8(5), Rules", "detail": "Back up the data folder (and the key, separately); set CABOT_BACKUPS_CONFIRMED=1 once restore is tested."},
    ]
