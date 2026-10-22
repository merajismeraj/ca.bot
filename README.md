# CA Brain — the compliance brain for an Indian CA office

Dump client data once. CA Brain sorts every file to the right client, document type and month, builds each client's
FY compliance calendar (GST, Income tax, TDS/TCS, ROC, payroll), works out what is missing for every filing and tells
each person in the office what to do next. Nothing is filed, extended, approved or sent to a client without a human click.

```
 drop files ─▶ classify (client · type · period) ─▶ vault ─▶ readiness per filing ─▶ next action per person
   zip/folders     PAN/GSTIN · name · learned aliases     month grid    missing docs, blockers     chase · prepare · review · approve · file
                          │ low confidence                                                              │
                          └────────────▶ Inbox review (human) ── teaches the classifier                 └─ partner gate + ack no.
```

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000          # open http://localhost:8000
```

First screen offers **Try with demo data** (six clients, ~110 synthetic files, a few deliberate gaps).
Or: `docker build -t ca-brain . && docker run -p 8000:8000 -v $PWD/data:/data ca-brain`.

| Env var | Default | Purpose |
|---|---|---|
| `CABOT_DATA` | `./data` | SQLite DB + uploaded files (back up this folder) |
| `ANTHROPIC_API_KEY` | – | Enables free-form questions in the command bar (Claude, tool use). Without it a built-in parser handles the common phrasings offline |
| `CABOT_MODEL` | `claude-opus-5-5` | Model for the command bar |
| `CABOT_LLM` | `1` | `0` forces offline mode |
| `CABOT_TODAY` | today | Pin the date (demos/tests) |

Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest`

## What the office sees

- **Today** — one list, sorted by urgency, grouped Overdue / This week / Next 30 days. Each row: readiness ring, stage,
  due date and the one button that moves it forward. Partners see *Needs your approval* first.
- **Inbox** — drop zips, folders or files anywhere in the app. Confident matches file themselves; the rest show up with
  the system's best guess pre-filled and the reason it was unsure. Corrections are remembered.
- **Calendar** — month view of every due date across clients, with done/total per form. Government extensions are
  entered here (or typed into the command bar) and apply to every affected client once a partner approves.
  Each FY's calendar carries a partner sign-off.
- **Clients** — health at a glance; paste a GSTIN and PAN, state, entity and GST scheme fill themselves; a live preview
  shows which filings will be scheduled. Paste a whole client list in plain words to onboard an office in minutes.
  Per-client document vault shows a month-by-month grid of what has arrived.
- **Outbox** — one consolidated, polite document request per client (only for periods that have ended), edited and sent
  by a person via WhatsApp/email.
- **Command bar** (`/` or `⌘K`) — “what's due this week”, “missing docs for Mehta”, “chase Sharma”,
  “add client Verma Exports 07AABCV1234D1Z3 audit tds payroll”, “extend GSTR-9 FY 2025-26 to 31 Jan 2027 per notification …”.
  Reads answer instantly; changes come back as proposals with a Confirm button.

## Human-in-the-loop guardrails

| Step | Gate |
|---|---|
| File classification | Below 80 % confidence on client, type **or** period → Inbox review |
| Prepare → review | Blocked while dependencies are unfiled (e.g. GSTR-9 waits on all GSTR-1/3B) |
| Review → approved | Partner only |
| Approved → filed | Acknowledgement / ARN / SRN mandatory |
| N/A, reopen, send back | Partner only, reason mandatory |
| Due-date extension | Staff propose, partner approves; statutory default kept alongside |
| Client messages | Drafts only; a person sends from their own WhatsApp/email |
| AI command bar | Read-only tools + `propose_*` tools; execution goes through the same gates above |
| Everything | Activity log |

## Compliance coverage (`app/compliance.py`)

GSTR-1 / 3B (monthly and QRMP, 22nd vs 24th by state), PMT-06, CMP-08, GSTR-4, GSTR-9 (> ₹2 Cr), GSTR-9C (> ₹5 Cr),
TDS/TCS deposits (March → 30 April), quarterly TDS/TCS statements, Form 16, advance tax (incl. presumptive), tax audit,
ITR (31 Jul / 31 Oct / 30 Nov), DPT-3, DIR-3 KYC, ADT-1, AOC-4, MGT-7, LLP Form 8 & 11, PF, ESI.
Each rule declares the documents it needs, so readiness is computed, not guessed. Adding a rule is ~5 lines.

**Verify before relying on it.** Dates are statutory defaults; extensions are deliberately *not* assumed. The
Income-tax Act, 2025 applies from 1 April 2026 and renumbers several forms (TDS statements, tax audit report) — labels
here are kept generic; confirm form numbers and any changed due dates against current CBDT/CBIC/MCA notifications
and record changes as extensions. ROC dates assume an AGM on 30 September.

## Before going live

- **Authentication is not built in.** The "Working as" switcher is a role selector, not a login. Put the app behind
  your SSO / reverse proxy and map users, or add auth before exposing it beyond a trusted LAN.
- Client data is personal data under the DPDP Act, 2023: keep `CABOT_DATA` on an encrypted disk, back it up, restrict access.
- With `ANTHROPIC_API_KEY` set, the command bar sends client names, filing statuses and due dates (not file contents)
  to the Anthropic API.
- Scanned PDFs/images are classified from filename and folder only (no OCR yet) and usually land in the Inbox.

## Layout

```
app/fy.py           FY/AY/period helpers
app/compliance.py   rule engine: applicability, periods, due dates, required documents, dependencies
app/classifier.py   file → client / type / period with confidence + reasons; learning tokens
app/engine.py       calendar sync, readiness, workflow state machine, ingest, folder consensus, chase drafts
app/brain.py        command bar: offline parser + Claude tool-use (read tools, propose_* tools)
app/main.py         FastAPI routes
app/seed.py         demo office
static/             no-build UI (vanilla JS)
tests/              calendar, classifier, API workflow, LLM loop (faked)
```
