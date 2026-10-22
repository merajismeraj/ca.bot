"""The command bar: ask questions or give instructions in plain English.

Reads are answered immediately. Anything that changes data (new client,
due-date extension, status change, client chase) comes back as a *proposal*
that a person confirms with one click - the brain never acts on its own.

Two interpreters share the same read/propose functions:
  * a deterministic parser for the everyday phrasings (always on, free, offline)
  * Claude with tool use, when an API key is configured, for everything else
"""
from __future__ import annotations

import contextvars
import json
import os
import re
from datetime import date, timedelta

from . import classifier, db, engine
from . import fy as F
from .catalog import GST_STATES, PAN_ENTITY
from .compliance import RULES, RULES_BY_CODE

MODEL = os.environ.get("CABOT_MODEL", "claude-opus-5-5")

# client ids the person asking may see (None = all); set once per ask() so every read tool honours it
_scope: contextvars.ContextVar[set[int] | None] = contextvars.ContextVar("scope", default=None)


def _visible_clients(conn) -> list[dict]:
    scope = _scope.get()
    return [c for c in db.rows(conn, "SELECT * FROM clients WHERE archived=0") if scope is None or c["id"] in scope]

FORM_ALIASES = {
    "gstr-1": ["GSTR1_M", "GSTR1_Q"], "gstr1": ["GSTR1_M", "GSTR1_Q"],
    "gstr-3b": ["GSTR3B_M", "GSTR3B_Q"], "gstr3b": ["GSTR3B_M", "GSTR3B_Q"], "3b": ["GSTR3B_M", "GSTR3B_Q"],
    "gstr-9c": ["GSTR9C"], "gstr9c": ["GSTR9C"], "gstr-9": ["GSTR9"], "gstr9": ["GSTR9"],
    "gstr-4": ["GSTR4"], "cmp-08": ["CMP08"], "cmp08": ["CMP08"], "pmt-06": ["PMT06"],
    "tax audit": ["TAX_AUDIT"], "audit report": ["TAX_AUDIT"], "itr": ["ITR"], "income tax return": ["ITR"],
    "advance tax": ["ADV_TAX"], "24q": ["TDS_RET_SAL"], "26q": ["TDS_RET_NONSAL"],
    "tds return": ["TDS_RET_SAL", "TDS_RET_NONSAL"], "tds payment": ["TDS_PAY"], "tcs": ["TCS_RET"],
    "aoc-4": ["AOC4"], "aoc4": ["AOC4"], "mgt-7": ["MGT7"], "mgt7": ["MGT7"], "adt-1": ["ADT1"],
    "dir-3": ["DIR3KYC"], "dpt-3": ["DPT3"], "form 11": ["LLP11"], "form 8": ["LLP8"],
    "pf": ["PF"], "esi": ["ESI"], "form 16": ["FORM16"],
}


# ================================================================ reads
def n_filings(n: int) -> str:
    return f"{n} filing{'' if n == 1 else 's'}"


def find_clients(conn, query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    out = []
    qt = set(classifier.norm_name_tokens(q))
    for c in _visible_clients(conn):
        c = db.client_dict(c)
        names = [c["name"]] + c["aliases"]
        hay = " ".join(n.lower() for n in names)
        nt = set(t for n in names for t in classifier.norm_name_tokens(n))
        if q in hay or (c.get("pan") or "").lower() == q or (c.get("gstin") or "").lower() == q or (qt and qt <= nt):
            out.append(c)
    return out


def client_in_text(conn, text: str) -> dict | None:
    """Best client mentioned anywhere in a sentence."""
    tl = text.lower()
    toks = set(classifier.tokens(text))
    best, score = None, 0.0
    for c in _visible_clients(conn):
        c = db.client_dict(c)
        for n in [c["name"]] + c["aliases"]:
            nt = classifier.norm_name_tokens(n)
            if not nt:
                continue
            s = 1.0 if n.lower() in tl else sum(t in toks for t in nt) / len(nt)
            if s > score:
                best, score = c, s
        if c.get("pan") and c["pan"].lower() in tl:
            return c
    return best if score >= 0.5 else None


def list_obligations(conn, client_id=None, start=None, end=None, stages=None, limit=60) -> list[dict]:
    sql, args = "SELECT * FROM obligations WHERE status NOT IN ('not_tracked')", []
    if client_id:
        sql += " AND client_id=?"
        args.append(client_id)
    if start:
        sql += " AND due>=?"
        args.append(start)
    if end:
        sql += " AND due<=?"
        args.append(end)
    sql += " ORDER BY due, client_id"
    out = engine.enrich(conn, db.rows(conn, sql, args))
    scope = _scope.get()
    if scope is not None:
        out = [o for o in out if o["client_id"] in scope]
    if stages:
        out = [o for o in out if o["stage"] in stages]
    return out[:limit]


def brief(o: dict) -> dict:
    return {k: o[k] for k in ("id", "client_name", "form", "period_label", "due", "days_left", "stage", "readiness")} | {
        "missing": [f"{m['label']} ({m['period']})" for m in o["missing"]], "blockers": o["blockers"]}


# =============================================================== parsing
DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})"), lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
    (re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({classifier.MONTH_ALT})[a-z]*,?\s+(\d{{4}})", re.I),
     lambda m: date(int(m[3]), classifier.MONTH_NAMES[m[2].lower()], int(m[1]))),
    (re.compile(rf"({classifier.MONTH_ALT})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.I),
     lambda m: date(int(m[3]), classifier.MONTH_NAMES[m[1].lower()], int(m[2]))),
]


def parse_date(s: str) -> date | None:
    for rx, fn in DATE_PATTERNS:
        m = rx.search(s)
        if m:
            try:
                return fn(m)
            except (ValueError, KeyError):
                continue
    return None


def window(text: str) -> tuple[date, date, str] | None:
    t = engine.today()
    tl = text.lower()
    if "today" in tl:
        return t, t, "today"
    if "tomorrow" in tl:
        return t + timedelta(days=1), t + timedelta(days=1), "tomorrow"
    m = re.search(r"next\s+(\d{1,3})\s+days", tl)
    if m:
        return t, t + timedelta(days=int(m[1])), f"in the next {m[1]} days"
    if "next week" in tl:
        s = t + timedelta(days=7 - t.weekday())
        return s, s + timedelta(days=6), "next week"
    if re.search(r"\b(this|current)\s+week\b|^week\b", tl):
        return t, t + timedelta(days=6 - t.weekday()), "this week"
    if re.search(r"\b(this|current)\s+month\b", tl):
        nm = date(t.year + (t.month == 12), t.month % 12 + 1, 1)
        return t, nm - timedelta(days=1), "this month"
    return None


def parse_client_spec(text: str) -> dict:
    """'add client Sharma Traders PAN ABCPS1234K GST monthly audit tds' -> profile."""
    s = text.strip()
    s = re.sub(r"^(please\s+)?(add|create|onboard|new)\s+(a\s+)?(new\s+)?client\s*:?\s*", "", s, flags=re.I)
    spec: dict = {"gst_scheme": "none"}
    up = s.upper()
    g = classifier.GSTIN_RE.search(up)
    if g:
        spec["gstin"], spec["gst_state"], spec["pan"] = g.group(0), g.group(1), g.group(2)
        spec.setdefault("gst_scheme", "monthly")
        spec["gst_scheme"] = "monthly"
    p = classifier.PAN_RE.search(up)
    if p and "pan" not in spec:
        spec["pan"] = p.group(1)
    if spec.get("pan"):
        spec["entity_type"] = PAN_ENTITY.get(spec["pan"][3], "individual")
    tl = s.lower()
    for word, et in (("private limited", "company"), ("pvt ltd", "company"), (" ltd", "company"), ("company", "company"),
                     ("llp", "llp"), ("partnership", "firm"), (" firm", "firm"), ("huf", "huf"), ("trust", "trust"),
                     ("individual", "individual"), ("salaried", "individual")):
        if word in f" {tl}":
            spec["entity_type"] = et
            break
    if re.search(r"\bqrmp\b|quarterly gst", tl):
        spec["gst_scheme"] = "qrmp"
    elif "composition" in tl:
        spec["gst_scheme"] = "composition"
    elif re.search(r"\bgst\b.*monthly|monthly gst|\bgst\b", tl) and spec["gst_scheme"] == "none":
        spec["gst_scheme"] = "monthly"
    for st, name in GST_STATES.items():
        if name.lower() in tl and "gst_state" not in spec:
            spec["gst_state"] = st
    spec["tax_audit"] = bool(re.search(r"\b(tax )?audit\b", tl))
    spec["tds"] = "tds" in tl
    spec["tcs"] = bool(re.search(r"\btcs\b", tl))
    spec["payroll"] = bool(re.search(r"payroll|employees|\bpf\b|\besi\b", tl))
    spec["advance_tax"] = "advance tax" in tl
    spec["presumptive"] = bool(re.search(r"44ad|presumptive", tl))
    spec["salaried"] = "salaried" in tl or "salary" in tl
    tv = re.search(r"turnover\s*(?:of\s*)?(?:rs\.?|₹)?\s*([\d.]+)\s*(cr|crore|l|lakh)", tl)
    if tv:
        v = float(tv[1])
        spec["turnover_cr"] = v / 100 if tv[2].startswith("l") else v
    ph = re.search(r"(?:\+91[\s-]?)?\b[6-9]\d{9}\b", s)
    if ph:
        spec["phone"] = re.sub(r"\D", "", ph.group(0))[-10:]
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", s)
    if em:
        spec["email"] = em.group(0)
    # name = text before the first structured token
    cut = re.split(r"\b(pan|gstin|gst|with|turnover|tds|tcs|audit|payroll|qrmp|composition|monthly|phone|email|"
                   r"individual|salaried|advance)\b|[A-Z]{5}\d{4}[A-Z]|\d{2}[A-Z]{5}\d{4}|[,;(]", s, maxsplit=1, flags=re.I)
    spec["name"] = cut[0].strip(" -:") if cut and cut[0].strip() else None
    spec.setdefault("entity_type", "individual")
    return spec


def resolve_rules(text: str) -> list[str]:
    tl = text.lower()
    for alias in sorted(FORM_ALIASES, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", tl):
            return FORM_ALIASES[alias]
    return []


def default_fy_for(rule_code: str, period_key: str) -> int:
    """The live FY whose instance of this rule is next due."""
    t = engine.today()
    rule = RULES_BY_CODE[rule_code]
    probe = {"entity_type": "company", "gst_scheme": "monthly", "gst_state": "27", "turnover_cr": 10}
    best = None
    for fy in engine.live_fys():
        for inst in rule.instances(fy, probe):
            if period_key in ("*", inst.period_key) and inst.due >= t - timedelta(days=30):
                if best is None or inst.due < best[1]:
                    best = (fy, inst.due)
    return best[0] if best else engine.live_fys()[0]


def parse_override(text: str) -> list[dict]:
    codes = resolve_rules(text)
    new_due = parse_date(text.split(" to ")[-1]) or parse_date(text)
    if not codes or not new_due:
        return []
    tl = text.lower()
    period_key = "*"
    q = re.search(r"\bq([1-4])\b", tl)
    if q:
        period_key = f"Q{q[1]}"
    else:
        m = re.search(rf"\b(?:for\s+)({classifier.MONTH_ALT})[a-z]*\s*'?(\d{{2,4}})?", tl)
        if m:
            mo = classifier.MONTH_NAMES[m[1]]
            yr = int(m[2]) + (2000 if m[2] and len(m[2]) == 2 else 0) if m[2] else None
            if not yr:
                yr = new_due.year if mo <= new_due.month else new_due.year - 1
            period_key = F.month_key(yr, mo)
    fy = classifier.detect_fy(text)
    reason = re.search(r"(?:reason|as per|vide|per)\s*:?\s*(.+)$", text, re.I)
    out = []
    for code in codes:
        f = fy if fy is not None else (F.fy_of(date(*map(int, period_key.split("-")), 1))
                                       if re.match(r"\d{4}-\d{2}", period_key) else default_fy_for(code, period_key))
        out.append({"kind": "override_due", "payload": {
            "rule_code": code, "fy": f, "period_key": period_key, "new_due": new_due.isoformat(),
            "reason": (reason[1].strip() if reason else "Extension notified by government")},
            "summary": f"Move {RULES_BY_CODE[code].name} · {F.fy_label(f)}"
                       f"{'' if period_key == '*' else ' · ' + period_key} to {new_due.strftime('%d %b %Y')}"})
    return out


def local(conn, text: str, user: dict) -> dict | None:
    tl = text.lower().strip()
    t = engine.today()

    if re.match(r"^(please\s+)?(add|create|onboard|new)\s+(a\s+)?(new\s+)?client", tl):
        spec = parse_client_spec(text)
        if not spec.get("name"):
            return {"answer": "What's the client's name? Try: “Add client Sharma Traders PAN ABCPS1234K GST monthly, TDS”."}
        flags = [k.replace("_", " ") for k in ("tax_audit", "tds", "tcs", "payroll", "advance_tax", "presumptive") if spec.get(k)]
        summary = (f"Add {spec['name']} ({spec['entity_type']}"
                   f"{', GST ' + spec['gst_scheme'] if spec['gst_scheme'] != 'none' else ''}"
                   f"{', ' + ', '.join(flags) if flags else ''})")
        return {"answer": "Here's what I understood. Check and confirm — I'll build the FY compliance calendar right after.",
                "proposals": [{"kind": "add_client", "payload": spec, "summary": summary}]}

    if re.search(r"\b(extend|extension|extended|move|shift|postpone)\b", tl) and resolve_rules(tl):
        props = parse_override(text)
        if props:
            return {"answer": "Due-date change drafted. A partner must approve it before it reaches the calendar.",
                    "proposals": props}

    if re.match(r"^(chase|remind|follow up( with)?|ask)\b", tl):
        c = client_in_text(conn, text)
        if c:
            draft = engine.build_chase(conn, c["id"])
            if not draft:
                return {"answer": f"{c['name']} has nothing pending from their side in the next 45 days. 🎉"}
            return {"answer": f"Drafted a document request for {c['name']}.",
                    "proposals": [{"kind": "chase", "payload": {"client_id": c["id"], "body": draft["body"]},
                                   "summary": f"Queue WhatsApp/email to {c['name']} ({draft['items']} items)"}]}

    if "overdue" in tl or "late" in tl or "missed" in tl:
        c = client_in_text(conn, text)
        items = [o for o in list_obligations(conn, c["id"] if c else None, end=(t - timedelta(days=1)).isoformat())
                 if o["status"] not in engine.DONE]
        return {"answer": f"{len(items)} overdue{' for ' + c['name'] if c else ''}." if items else "Nothing overdue. 👏",
                "items": items}

    if re.search(r"missing|pending doc|waiting|documents? (needed|pending|required)|what do we need", tl):
        c = client_in_text(conn, text)
        items = [o for o in list_obligations(conn, c["id"] if c else None, end=(t + timedelta(days=45)).isoformat())
                 if o["stage"] == "awaiting_docs"]
        who = f" for {c['name']}" if c else ""
        return {"answer": f"{n_filings(len(items))}{who} {'is' if len(items) == 1 else 'are'} waiting on documents in the next 45 days." if items
                else f"No document gaps{who} for the next 45 days.", "items": items}

    if re.search(r"review|approve|approval|sign.?off", tl):
        items = list_obligations(conn, stages=("review",))
        return {"answer": f"{len(items)} waiting for partner approval." if items else "Nothing waiting for approval.",
                "items": items}

    w = window(tl)
    c = client_in_text(conn, text)
    if w or re.search(r"\bdue\b|deadline|upcoming|pending|status", tl) or (c and not llm_available()):
        start, end, label = w or (t - timedelta(days=60), t + timedelta(days=30), "in the next 30 days (and anything overdue)")
        items = [o for o in list_obligations(conn, c["id"] if c else None, start.isoformat(), end.isoformat())
                 if o["status"] not in engine.DONE]
        who = f" for {c['name']}" if c else ""
        ans = f"{n_filings(len(items))} due {label}{who}." if items else f"Nothing due {label}{who}."
        return {"answer": ans, "items": items}
    return None


# ================================================================ Claude
SYSTEM = """You are the operations brain of a Chartered Accountant (CA) office in India. Staff and partners ask you \
about client compliance (GST, income tax, TDS/TCS, ROC/MCA, payroll), deadlines, missing documents and work status.

Ground every answer in the tools; never invent clients, dates or statuses. Today is {today}; the current financial \
year is {fy}. Indian FY runs April-March.

You cannot change data yourself. To add a client, move a due date (government extension), change a filing's \
status or chase a client, call the matching propose_* tool; a human confirms it in the UI. Statutory due dates in \
the calendar are defaults - only propose an override when the user cites an extension.

Reply in at most 4 short sentences or a tight bullet list. Use Indian formats (₹, DD Mon YYYY). If the user asks \
for something outside the office's data, say so briefly."""


def _tools():
    obj = lambda props, req: {"type": "object", "properties": props, "required": req, "additionalProperties": False}
    s = {"type": "string"}
    return [
        {"name": "find_clients", "description": "Search clients by name, alias, PAN or GSTIN.",
         "input_schema": obj({"query": s}, ["query"])},
        {"name": "list_obligations",
         "description": "List compliance obligations with stage (awaiting_docs, blocked, ready, in_progress, review, "
                        "approved, filed, na), readiness %, missing documents and days left. Dates are YYYY-MM-DD.",
         "input_schema": obj({"client_id": {"type": "integer"}, "due_from": s, "due_to": s,
                              "stages": {"type": "array", "items": s}}, [])},
        {"name": "list_rules", "description": "List the compliance rule codes and forms the calendar knows about.",
         "input_schema": obj({}, [])},
        {"name": "propose_add_client", "description": "Propose onboarding a client. Human confirms.",
         "input_schema": obj({
             "name": s, "pan": s, "gstin": s, "entity_type": {"type": "string", "enum": ["individual", "huf", "firm", "llp", "company", "trust"]},
             "gst_scheme": {"type": "string", "enum": ["none", "monthly", "qrmp", "composition"]}, "gst_state": s,
             "turnover_cr": {"type": "number"}, "tax_audit": {"type": "boolean"}, "tds": {"type": "boolean"},
             "tcs": {"type": "boolean"}, "payroll": {"type": "boolean"}, "advance_tax": {"type": "boolean"},
             "presumptive": {"type": "boolean"}, "salaried": {"type": "boolean"}, "phone": s, "email": s,
             "contact_name": s}, ["name", "entity_type"])},
        {"name": "propose_due_override",
         "description": "Propose moving a statutory due date (e.g. CBDT/CBIC extension). period_key is '*' for all "
                        "periods, 'YYYY-MM' for a month, 'Q1'-'Q4', 'FY' or 'I1'-'I4' (advance tax). fy is the FY start year.",
         "input_schema": obj({"rule_code": s, "fy": {"type": "integer"}, "period_key": s, "new_due": s, "reason": s},
                             ["rule_code", "fy", "period_key", "new_due", "reason"])},
        {"name": "propose_status_change",
         "description": "Propose a workflow action on one obligation: start, submit, approve, send_back, file, mark_na, reopen. "
                        "'file' needs ack_no; mark_na/reopen/send_back need note.",
         "input_schema": obj({"obligation_id": {"type": "integer"}, "action": s, "ack_no": s, "note": s},
                             ["obligation_id", "action"])},
        {"name": "propose_chase", "description": "Draft a consolidated document request to a client.",
         "input_schema": obj({"client_id": {"type": "integer"}}, ["client_id"])},
    ]


def _run_tool(conn, name: str, args: dict, proposals: list, items: dict) -> object:
    if name == "find_clients":
        return [{k: c[k] for k in ("id", "name", "pan", "gstin", "entity_type", "gst_scheme")} for c in find_clients(conn, args["query"])][:10]
    if name == "list_obligations":
        res = list_obligations(conn, args.get("client_id"), args.get("due_from"), args.get("due_to"), args.get("stages"))
        for o in res:
            items[o["id"]] = o
        return [brief(o) for o in res[:40]]
    if name == "list_rules":
        return [{"code": r.code, "name": r.name, "form": r.form, "authority": r.authority} for r in RULES]
    if name == "propose_add_client":
        proposals.append({"kind": "add_client", "payload": args, "summary": f"Add client {args['name']} ({args['entity_type']})"})
        return "Proposal shown to the user for confirmation."
    if name == "propose_due_override":
        if args["rule_code"] not in RULES_BY_CODE:
            return {"error": "unknown rule_code; call list_rules"}
        d = date.fromisoformat(args["new_due"])
        proposals.append({"kind": "override_due", "payload": args,
                          "summary": f"Move {RULES_BY_CODE[args['rule_code']].name} · {F.fy_label(args['fy'])}"
                                     f"{'' if args['period_key'] == '*' else ' · ' + args['period_key']} to {d.strftime('%d %b %Y')}"})
        return "Proposal shown; needs partner approval."
    if name == "propose_status_change":
        o = db.row(conn, "SELECT * FROM obligations WHERE id=?", (args["obligation_id"],))
        if not o:
            return {"error": "no such obligation"}
        e = engine.enrich(conn, [o])[0]
        proposals.append({"kind": "transition", "payload": args,
                          "summary": f"{args['action'].replace('_', ' ').title()}: {e['form']} {e['period_label']} · {e['client_name']}"})
        return "Proposal shown to the user for confirmation."
    if name == "propose_chase":
        draft = engine.build_chase(conn, args["client_id"])
        if not draft:
            return "Nothing pending from this client."
        proposals.append({"kind": "chase", "payload": {"client_id": args["client_id"], "body": draft["body"]},
                          "summary": f"Queue message to {draft['client']['name']} ({draft['items']} items)"})
        return {"draft": draft["body"]}
    return {"error": f"unknown tool {name}"}


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) \
        and os.environ.get("CABOT_LLM", "1") != "0"


def ask_claude(conn, text: str, history: list[dict]) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    system = SYSTEM.format(today=engine.today().strftime("%d %b %Y"), fy=F.fy_label(F.fy_of(engine.today())))
    messages = [*history[-6:], {"role": "user", "content": text}]
    proposals: list = []
    items: dict = {}
    for _ in range(8):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=4000, system=system, tools=_tools(), messages=messages,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            return {"answer": "I can't help with that one.", "proposals": proposals, "items": list(items.values())}
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            answer = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"answer": answer or "Done.", "proposals": proposals, "items": list(items.values())[:30], "via": "claude"}
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = _run_tool(conn, b.name, dict(b.input), proposals, items)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)})
            except Exception as e:  # bad arguments from the model: report back, let it retry
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(e), "is_error": True})
        messages.append({"role": "user", "content": results})
    return {"answer": "That took too many steps — try a narrower question.", "proposals": proposals,
            "items": list(items.values())}


def ask(conn, text: str, user: dict, history: list[dict] | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"answer": "Ask me anything — “what's due this week”, “missing docs for Sharma”, "
                          "“extend GSTR-9 FY 2025-26 to 31 Jan 2027”."}
    from . import auth
    token = _scope.set(auth.visible_client_ids(conn, user))
    try:
        return _ask(conn, text, user, history)
    finally:
        _scope.reset(token)


def _ask(conn, text: str, user: dict, history: list[dict] | None) -> dict:
    res = local(conn, text, user)
    if res is not None:
        res.setdefault("via", "rules")
    elif llm_available():
        try:
            res = ask_claude(conn, text, history or [])
        except Exception as e:  # network / auth / rate limit - stay useful offline
            res = {"answer": f"I couldn't reach the AI service ({type(e).__name__}). Try a simpler phrasing like "
                             "“due this week” or “missing docs for <client>”.", "via": "error"}
    else:
        res = {"answer": "I didn't catch that. Try “due this week”, “overdue”, “missing docs for <client>”, "
                         "“chase <client>”, “add client …” or “extend GSTR-9 to 31 Jan 2027”. "
                         "Set ANTHROPIC_API_KEY to unlock free-form questions.", "via": "rules"}
    res["items"] = [brief(o) | {"rule_name": o["rule_name"], "next_action": o["next_action"]} for o in res.get("items", [])]
    res.setdefault("proposals", [])
    return res
