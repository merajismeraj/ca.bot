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

The first visit sets up the office and its first partner login. Then **Try with demo data** loads six clients, ~110
synthetic files (including a phone photo that only OCR can read), and two staff logins to try permissions with.
Install `tesseract-ocr` (and `tesseract-ocr-hin`) for scanned documents — the Docker image includes both:
`docker build -t ca-brain . && docker run -p 8000:8000 -v $PWD/data:/data ca-brain`.

| Env var | Default | Purpose |
|---|---|---|
| `CABOT_DATA` | `./data` | SQLite DB + uploaded files (back up this folder) |
| `ANTHROPIC_API_KEY` | – | Enables free-form questions in the command bar (Claude, tool use). Without it a built-in parser handles the common phrasings offline |
| `CABOT_MODEL` | `claude-opus-5-5` | Model for the command bar |
| `CABOT_LLM` | `1` | `0` forces offline mode |
| `CABOT_TODAY` | today | Pin the date (demos/tests) |
| `CABOT_SECURE_COOKIES` | `0` | Set `1` when served over HTTPS |
| `CABOT_DATA_KEY` | – | 32-byte base64 key; encrypts client files at rest (AES-256-GCM) |
| `CABOT_PROVIDER_NAME` | "the provider of this software" | Name used in the Terms and Privacy Policy |
| `CABOT_BACKUPS_CONFIRMED` | – | Set `1` once encrypted backups and a restore have been tested (checklist item) |
| `CABOT_VISION_OCR` | `0` | `1` sends scans Tesseract can't read to Claude vision (file contents leave the office) |
| `CABOT_OCR_LANGS` | `eng`+`hin` if installed | Tesseract languages |
| `CABOT_MAX_UPLOAD_MB` / `CABOT_ZIP_MAX_MB` / `CABOT_ZIP_MAX_FILES` | 4096 / 4096 / 10000 | Upload and zip limits |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | – | Sign in with Google (both required) |
| `GOOGLE_ALLOWED_DOMAIN` | – | Only accept Google Workspace accounts on this domain, e.g. `yourfirm.in` |
| `CABOT_PUBLIC_URL` | from request | Public https address; used for the Google redirect URI and links in notices |
| `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_VERIFY_TOKEN`, `WA_APP_SECRET` | – | WhatsApp Business Cloud API (all four required) |
| `WA_TEMPLATE`, `WA_TEMPLATE_LANG`, `WA_API_VERSION` | –, `en`, `v21.0` | Approved template for messages outside the 24 h window; Graph API version |

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

## Logins and permissions

Every person signs in with their own email and password (scrypt-hashed; sessions are server-side tokens in an
HttpOnly cookie, or `Authorization: Bearer` for scripts). Partners invite colleagues from **Settings → Team**; the
invite produces a one-time password that must be changed on first sign-in. Five failed attempts lock that email for
15 minutes. Partners hold every permission; for staff each one is a checkbox:

| Permission | Lets them |
|---|---|
| Approve | approve filings, due-date extensions, N/A and reopenings, sign off the FY calendar |
| Clients | add, edit clients |
| Message | send messages to clients (others can draft) |
| All clients | see every client — otherwise only clients or filings assigned to them, everywhere including the command bar |
| Export | download client files as zip |
| Team | invite people, set permissions, change office settings |

### Sign in with Google

Invite-only: a Google account gets in only if its verified email matches an active team member a partner has
invited (the account is then linked, and later sign-ins match it even if the email changes). Strangers with a
Google account are turned away. Optionally lock it to your Google Workspace domain. Email + password keeps working,
so a Google outage or misconfiguration can't lock the office out.

Setup: in Google Cloud Console → APIs & Services → Credentials, create an **OAuth client ID** (type *Web application*),
add the redirect URI shown in **Settings → Connections** (`https://<your-host>/api/auth/google/callback`), and set
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. The consent screen only needs the `openid`, `email` and `profile` scopes.
The flow uses PKCE, single-use state bound to the browser (stops login CSRF), and checks issuer, audience, expiry,
nonce, verified email and Workspace domain.

## Zips in, zips out

- **In:** drop any mix of files, folders and zips. Zips inside zips (3 levels), Windows backslash paths, non-UTF-8
  file names, WhatsApp "Export chat" zips (attachments kept, chat text skipped), Mac/Windows junk files ignored.
  Password-protected entries, zip bombs, oversize files and unsupported types are listed with a reason instead of
  failing the whole upload. Large zips are read entry by entry from disk, never loaded into memory.
- **Out:** **Download zip** on a client (or **Zip all** on Clients) gives `Client / FY / Document type / YYYY-MM file`
  plus `_index.csv`, `_filings.csv` (every filing with due date, status and ack no.) and `_profile.json`.

## Scanned documents (OCR)

Photos and image-only PDFs that can't be identified from their name are read in the background by Tesseract
(on the server, nothing leaves the office) and re-classified as soon as the text is in. The Inbox shows
"Reading the scan…" meanwhile. WhatsApp/phone/scanner filename stamps (`IMG-20260914-WA0003`, `Scan 2026-09-14`)
are recognised as the date sent, not the accounting period. A person's decision is never overwritten by OCR.

## Two-way WhatsApp

With the WhatsApp Business Cloud API connected, set the webhook in Meta to `https://<your-host>/api/whatsapp/webhook`
(verify token = `WA_VERIFY_TOKEN`; every delivery is checked against `WA_APP_SECRET`).

- Client sends a PDF, photo or zip to the office number → matched to the client by phone → filed through the same
  pipeline (OCR included). Unknown numbers land in the Inbox unassigned.
- Text replies show under the client's **Messages** tab and as "New from clients" in the Outbox.
- **Send on WhatsApp** on a draft sends from the office number. Inside 24 h of the client's last message it is a
  normal message; outside it, Meta requires an approved template (`WA_TEMPLATE`, one body parameter that receives
  the list of documents) — otherwise the app says so and offers the "Open WhatsApp" link from your phone.

## Data protection (DPDP Act, 2023)

The CA firm running this software is the **Data Fiduciary** for its clients' personal data. The software gives it
the controls and records the Act and the DPDP Rules, 2025 expect; it does not make the firm compliant by itself
(see the Terms). Partners manage everything from the **Privacy** page:

| Obligation | What the software does |
|---|---|
| Notice & consent (s.5, s.6) | Drafts a privacy notice per client (links to `/privacy`), records consent with channel and purposes, consent log |
| Withdrawal (s.6(4),(6)) | One click / request; then no chasing, no messages (except breach notices), no new uploads, attachments not stored |
| Children (s.9) | Minor flag; consent can't be recorded without a parent/guardian |
| Rights (s.11–14) | Request register (access, correction, erasure, withdrawal, grievance, nomination) with response deadline (≤ 90 days), overdue alerts, access summary + data copy as zip |
| Retention & erasure (s.8(7), s.12) | Erasure keeps only records tax / GST / company law require (default 8 years after FY, configurable), anonymises the profile when nothing is held, retention review to purge expired files — always a person's click |
| Security (s.8(5), Rules) | AES-256-GCM encryption at rest (`CABOT_DATA_KEY`), per-person logins and client scoping, document-access logging, activity log kept ≥ 1 year |
| Breach (s.8(6), Rules) | Breach register with the 72-hour Board-report clock, client notice drafts |
| Grievance contact (s.8(9)) | Officer name / email / phone published on the policy |
| Readiness | Checklist of what's still missing (officer, address, consent coverage, encryption, HTTPS, backups, overdue requests, breaches, retention) |

Public pages, no login: **`/privacy`** (Privacy & Data Protection Policy, filled with the office's details; anything
not yet set is highlighted) and **`/terms`** (Terms of Use & Disclaimer). The first partner must accept the Terms when
setting up the office; acceptance is recorded. Set `CABOT_PROVIDER_NAME` to name the software provider in both pages.

Generate an encryption key with `python -m app.storage new-key`, keep it outside the data folder, and convert files
uploaded earlier with `python -m app.storage encrypt-all`.

**Get both pages reviewed by a lawyer before go-live.** They are templates. A disclaimer limits liability only as far
as Indian law allows — it cannot remove the firm's statutory duties under the DPDP Act or the penalties for breaching
them. Check the DPDP Rules' commencement dates for each obligation.

## Human-in-the-loop guardrails

| Step | Gate |
|---|---|
| File classification | Below 80 % confidence on client, type **or** period → Inbox review |
| Prepare → review | Blocked while dependencies are unfiled (e.g. GSTR-9 waits on all GSTR-1/3B) |
| Review → approved | Approve permission (partners) |
| Approved → filed | Acknowledgement / ARN / SRN mandatory |
| N/A, reopen, send back | Approve permission, reason mandatory |
| Due-date extension | Staff propose, partner approves; statutory default kept alongside |
| Client messages | Drafts only; a person with Message permission sends (WhatsApp API, wa.me link or email) |
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

- Serve over HTTPS (reverse proxy) and set `CABOT_SECURE_COOKIES=1`. WhatsApp webhooks need a public HTTPS URL.
- Fill in the Privacy page (grievance officer, address, jurisdiction, policy date) and clear the checklist.
- With `ANTHROPIC_API_KEY` set, the command bar sends client names, filing statuses and due dates (not file contents)
  to the Anthropic API.
- No 2FA or SSO yet; deactivating a person signs them out everywhere immediately.

## Layout

```
app/privacy.py      DPDP: consent, rights requests, erasure with legal hold, retention, breaches, checklist
app/storage.py      encrypted file storage
app/auth.py         logins, sessions, permissions, client scoping
app/archive.py      zip extraction (nested, Windows, WhatsApp exports, safety limits) and organised export
app/ocr.py          Tesseract / Claude-vision OCR and the background worker
app/whatsapp.py     WhatsApp Cloud API webhook, media download, sending within Meta's rules
app/fy.py           FY/AY/period helpers
app/compliance.py   rule engine: applicability, periods, due dates, required documents, dependencies
app/classifier.py   file → client / type / period with confidence + reasons; learning tokens
app/engine.py       calendar sync, readiness, workflow state machine, ingest, folder consensus, chase drafts
app/brain.py        command bar: offline parser + Claude tool-use (read tools, propose_* tools)
app/main.py         FastAPI routes
app/seed.py         demo office
static/             no-build UI (vanilla JS); privacy.html / terms.html public policy pages
tests/              calendar, classifier, workflow, auth, zips, OCR, WhatsApp (Meta faked), LLM loop (faked)
```
