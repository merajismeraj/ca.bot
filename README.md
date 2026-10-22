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

Sign in with Google and create your firm. Then **Try with demo data** loads six clients, ~110 synthetic files
(including a phone photo that only OCR can read) and two team members — a Manager, and an Article on selected clients.
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
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | – | Sign in with Google — required, it is the only way in. Create a *Web application* OAuth client and register `https://<host>/api/auth/google/callback` |
| `CABOT_PLATFORM_ADMINS` | – | Comma-separated Google emails of platform owners |
| `CABOT_OPEN_SIGNUP` | `0` | `1` = new firms are active immediately instead of awaiting approval |
| `CABOT_DEV_LOGIN` | `0` | `1` = localhost-only sign-in without Google, for development |
| `CABOT_PROVIDER_CONTACT_EMAIL` / `_NAME` / `_PHONE`, `CABOT_PROVIDER_ADDRESS`, `CABOT_JURISDICTION`, `CABOT_POLICY_UPDATED` | – | The platform's own privacy notice and Terms |
| `GOOGLE_ALLOWED_DOMAIN` | – | Only accept Google Workspace accounts on one domain (for a single-firm deployment) |
| `CABOT_PUBLIC_URL` | from request | Public https address; used for the Google redirect URI and links in notices |
| `WA_TOKEN`, `WA_VERIFY_TOKEN`, `WA_APP_SECRET` | – | The platform's WhatsApp Business Cloud API app; each firm enters its own phone number ID in Settings |
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

## Firms, sign-in and access

One platform, many CA firms, each fully isolated: every firm has its **own database file and upload folder**
(`data/firms/<id>/`). Requests are bound to the signed-in person's firm before any query runs, so one firm's data
can't reach another through a missed filter. A small platform database holds only firms, memberships and sessions.

**Sign-in is Google only** (OpenID Connect, PKCE, browser-bound single-use state; issuer, audience, expiry, nonce and
verified email checked). No passwords exist anywhere.

1. **A firm signs up.** Anyone signs in with Google and creates a firm workspace (accepting the Terms). They become
   its **Super admin**. Unless `CABOT_OPEN_SIGNUP=1`, the firm stays *pending* until a platform owner approves it.
2. **Platform owners** (`CABOT_PLATFORM_ADMINS=you@company.in`) get a console to approve, suspend (signs everyone out)
   and set up firms for customers. They see firm names, team and client *counts* — never client data.
3. **The super admin builds the team.** Add a person by their Google email, pick a **level**, and choose **which
   client accounts** they can see. They just "Continue with Google". Removing someone ends their sessions at once.
   People in several firms pick a firm after sign-in and can switch.

| Level | Default permissions | Default client access |
|---|---|---|
| Super admin | everything (fixed); only super admins can create or change super admins | all |
| Partner | documents, work, approve, clients, message, export, privacy | all |
| Manager | documents, work, clients, message, export | all |
| Staff | documents, work, message | selected |
| Article / trainee | documents, work | selected |
| Viewer (read-only) | — (sees filings and calendar, not documents) | selected |

Permissions: **Documents** (open/download files) · **Work** (upload, sort the Inbox, move filings) · **Approve** ·
**Clients** (add/edit profiles) · **Message** · **Export** · **Privacy** (DPDP requests, breaches, erasure) · **Team**.
Each firm can retune what every level can do; any person can also get a custom set. Client access is *all* or
*selected accounts* (plus anything assigned to them) and applies everywhere — lists, calendar, documents, the
command bar. A firm always keeps at least one active super admin.

**Local development without Google:** `CABOT_DEV_LOGIN=1` adds a "sign in without Google" box that works only from
localhost and never when `CABOT_SECURE_COOKIES=1`. `python -m app.seed [owner@email]` creates a demo firm.

**Upgrading a single-office install:** on first start the old `data/cabot.db` and uploads become firm #1, people
keep their access (the first partner becomes super admin) and now sign in with Google using their email.

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

Public pages, no login: **`/privacy?firm=<slug>`** (each firm's own Privacy & Data Protection Policy, filled with
its details; anything not yet set is highlighted — linked from client notices), **`/privacy`** (the platform's notice
for people who sign in) and **`/terms`** (Terms of Use & Disclaimer). Whoever creates a firm must accept the Terms;
acceptance is recorded. Set `CABOT_PROVIDER_NAME` to name the software provider in these pages.

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
app/tenancy.py      firms, memberships, sessions, platform owners, single-office upgrade
app/google_auth.py  Sign in with Google (OIDC + PKCE)
app/auth.py         levels, sessions, permissions, client scoping
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
