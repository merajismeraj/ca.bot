"""Two-way WhatsApp through the WhatsApp Business Cloud API (Meta).

Inbound  : clients reply to the office number with PDFs, photos or text. The
           webhook matches the sender's phone to a client, downloads each
           attachment and drops it into the same ingest pipeline as an upload
           (with the client as a strong hint). Text replies are kept in the
           client's conversation in the Outbox.
Outbound : a person clicks "Send on WhatsApp" on a draft. Meta only allows a
           free-form message within 24 h of the client's last message; outside
           that window we say so and the person uses the wa.me link (or an
           approved template, if configured) instead. Nothing is ever sent
           without a click.

Config (env): WA_TOKEN, WA_PHONE_NUMBER_ID, WA_VERIFY_TOKEN, WA_APP_SECRET,
optional WA_API_VERSION, WA_TEMPLATE (name of an approved template with one
body parameter, used outside the 24 h window), WA_TEMPLATE_LANG.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import db, engine

log = logging.getLogger("cabot.whatsapp")
GRAPH = "https://graph.facebook.com"
MIME_EXT = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "text/csv": ".csv", "text/plain": ".txt", "application/zip": ".zip",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def cfg(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def configured() -> bool:
    return bool(cfg("WA_TOKEN") and cfg("WA_PHONE_NUMBER_ID") and cfg("WA_APP_SECRET") and cfg("WA_VERIFY_TOKEN"))


def api_url(path: str) -> str:
    return f"{GRAPH}/{cfg('WA_API_VERSION', 'v21.0')}/{path}"


def headers() -> dict:
    return {"Authorization": f"Bearer {cfg('WA_TOKEN')}"}


def verify_signature(body: bytes, header: str | None) -> bool:
    secret = cfg("WA_APP_SECRET")
    if not secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])


def local_phone(wa_id: str) -> str:
    return re.sub(r"\D", "", wa_id or "")[-10:]


def client_for_phone(conn, wa_id: str) -> dict | None:
    p = local_phone(wa_id)
    return db.row(conn, "SELECT * FROM clients WHERE phone=? AND archived=0", (p,)) if p else None


# ---------------------------------------------------------------- inbound
def download_media(media_id: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=60) as h:
        meta = h.get(api_url(media_id), headers=headers())
        meta.raise_for_status()
        info = meta.json()
        f = h.get(info["url"], headers=headers())
        f.raise_for_status()
        return f.content, info.get("mime_type", "")


def handle_webhook(payload: dict) -> list[dict]:
    """Parse a Cloud API webhook body into work items. Pure: no network, no DB."""
    items = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind = m.get("type")
                item = {"wa_message_id": m.get("id"), "from": m.get("from"), "profile": names.get(m.get("from")),
                        "kind": kind, "ts": m.get("timestamp")}
                if kind == "text":
                    item["text"] = m.get("text", {}).get("body", "")
                elif kind in ("document", "image"):
                    media = m.get(kind, {})
                    item.update(media_id=media.get("id"), mime=media.get("mime_type", ""),
                                filename=media.get("filename"), text=media.get("caption", ""))
                else:
                    item["text"] = f"[{kind} message]"
                items.append(item)
    return items


def process(items: list[dict]) -> None:
    """Store texts, download and ingest attachments. Runs after the webhook has already answered 200."""
    for it in items:
        with db.tx() as c:
            if it["wa_message_id"] and db.row(c, "SELECT id FROM messages WHERE wa_message_id=?", (it["wa_message_id"],)):
                continue  # Meta retries deliveries; process each message once
            client = client_for_phone(c, it["from"])
            cid = client["id"] if client else None
            who = client["name"] if client else (it.get("profile") or f"+{it['from']}")
            body = it.get("text") or ""
            if it["kind"] in ("document", "image"):
                body = (f"📎 {it.get('filename') or it['kind']}" + (f" — {body}" if body else ""))
            c.execute("INSERT INTO messages(client_id,channel,subject,body,status,created_at,direction,from_phone,wa_message_id) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (cid, "whatsapp", None, body, "received", db.now(), "in", local_phone(it["from"]), it["wa_message_id"]))
            db.log(c, None, "wa_in", "client" if cid else "whatsapp", cid, f"WhatsApp from {who}: {body[:80]}")
        if it["kind"] not in ("document", "image") or not it.get("media_id"):
            continue
        with db.tx() as c:
            from . import privacy
            if privacy.withdrawn(c, cid):
                db.log(c, None, "wa_in", "client", cid, "Attachment not stored: client has withdrawn consent")
                continue
        try:
            data, mime = download_media(it["media_id"])
        except Exception as e:
            log.warning("media download failed: %s", e)
            continue
        ext = Path(it.get("filename") or "").suffix.lower() or MIME_EXT.get((mime or it.get("mime") or "").split(";")[0], "")
        stem = Path(it.get("filename") or "").stem or f"{it['kind']}-{it['wa_message_id'][-8:]}"
        caption = re.sub(r"[\\/:*?\"<>|]", " ", it.get("text") or "")[:80].strip()
        name = f"{stem}{(' ' + caption) if caption and caption.lower() not in stem.lower() else ''}{ext}"
        folder = f"WhatsApp/{who}"
        batch = f"wa-{datetime.now():%Y%m%d}"
        with db.tx() as c:
            if ext == ".zip":
                import io
                from . import archive
                for e in archive.expand(name, io.BytesIO(data)):
                    if e.data is not None:
                        engine.ingest(c, f"{folder}/{e.path}", e.data, None, batch, cid, source="whatsapp")
            else:
                engine.ingest(c, f"{folder}/{name}", data, None, batch, cid, source="whatsapp")
            engine.folder_consensus(c, batch)


# --------------------------------------------------------------- outbound
def last_inbound(conn, phone: str) -> datetime | None:
    r = db.row(conn, "SELECT MAX(created_at) at FROM messages WHERE direction='in' AND from_phone=?", (phone,))
    return datetime.fromisoformat(r["at"]) if r and r["at"] else None


def window_open(conn, phone: str) -> bool:
    at = last_inbound(conn, phone)
    return bool(at and datetime.now() - at < timedelta(hours=24))


class SendError(Exception):
    pass


def send(conn, phone: str, body: str) -> str:
    """Send a message; returns the WhatsApp message id. Raises SendError with an actionable reason."""
    if not configured():
        raise SendError("WhatsApp Business API isn't connected — use “Open WhatsApp” instead")
    to = cfg("WA_COUNTRY_CODE", "91") + phone
    if window_open(conn, phone):
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body[:4096]}}
    elif cfg("WA_TEMPLATE"):
        # templates can't carry line breaks in parameters: send the request list as one line
        flat = " · ".join(l.strip("• ").strip() for l in body.splitlines() if l.strip().startswith("•"))
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": cfg("WA_TEMPLATE"), "language": {"code": cfg("WA_TEMPLATE_LANG", "en")},
                                "components": [{"type": "body", "parameters": [{"type": "text", "text": flat[:1000] or "documents"}]}]}}
    else:
        raise SendError("The client hasn't messaged in the last 24 hours, so WhatsApp only allows an approved template. "
                        "Use “Open WhatsApp” to send from your phone, or set WA_TEMPLATE.")
    try:
        r = httpx.post(api_url(f"{cfg('WA_PHONE_NUMBER_ID')}/messages"), headers=headers(), json=payload, timeout=30)
    except httpx.HTTPError as e:
        raise SendError(f"Couldn't reach WhatsApp ({type(e).__name__})") from e
    if r.status_code >= 400:
        err = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("error", {})
        raise SendError(f"WhatsApp refused the message: {err.get('message') or r.status_code}")
    return r.json().get("messages", [{}])[0].get("id", "")
