"""Sign in with Google (OpenID Connect, authorization-code flow with PKCE) - the only way in.

Google proves who someone is; which firms they may enter is decided by each
firm's super admin (memberships), and creating a new firm needs platform approval.
Optionally restrict sign-in to one Google Workspace domain (single-firm deployments).

Config (env): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, optional
GOOGLE_ALLOWED_DOMAIN (e.g. yourfirm.in), CABOT_PUBLIC_URL (https://... used to
build the redirect URI; otherwise taken from the request).

The ID token is obtained directly from Google's token endpoint over TLS using
our client secret, so per OpenID Connect Core 3.1.3.7 its signature need not be
re-verified; issuer, audience, expiry, nonce and email verification are checked.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from . import db, tenancy

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_COOKIE = "cab_oauth"
STATE_TTL = timedelta(minutes=10)


class GoogleError(Exception):
    """A sign-in failure with a message safe to show to the person signing in."""


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def allowed_domain() -> str | None:
    d = (os.environ.get("GOOGLE_ALLOWED_DOMAIN") or "").strip().lower()
    return d or None


def redirect_uri(request_base: str) -> str:
    base = (os.environ.get("CABOT_PUBLIC_URL") or request_base).rstrip("/")
    return f"{base}/api/auth/google/callback"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def start(request_base: str) -> tuple[str, str]:
    """Return (Google URL to redirect to, browser-binding value for a cookie)."""
    state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
    with tenancy.tx() as conn:
        conn.execute("DELETE FROM oauth_states WHERE created_at<?", ((datetime.now() - STATE_TTL).isoformat(timespec="seconds"),))
        conn.execute("INSERT INTO oauth_states(state,nonce,verifier,browser,created_at) VALUES(?,?,?,?,?)",
                     (state, nonce, verifier, hashlib.sha256(browser.encode()).hexdigest(), db.now()))
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri(request_base),
        "response_type": "code", "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if allowed_domain():
        params["hd"] = allowed_domain()  # only a hint to Google's account chooser; enforced again on return
    return f"{AUTH_URL}?{urlencode(params)}", browser


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise GoogleError("Google sent an unreadable sign-in token") from e


def finish(code: str, state: str, browser: str | None, request_base: str) -> dict:
    """Validate Google's response; return the verified identity {email, sub, name}."""
    with tenancy.tx() as conn:
        row = db.row(conn, "SELECT * FROM oauth_states WHERE state=?", (state or "",))
        conn.execute("DELETE FROM oauth_states WHERE state=?", (state or "",))  # single use
    if not row or datetime.fromisoformat(row["created_at"]) < datetime.now() - STATE_TTL:
        raise GoogleError("This sign-in link expired. Please try again.")
    if not browser or hashlib.sha256(browser.encode()).hexdigest() != row["browser"]:
        raise GoogleError("Sign-in was started in a different browser. Please try again.")
    try:
        r = httpx.post(TOKEN_URL, data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": redirect_uri(request_base), "grant_type": "authorization_code", "code_verifier": row["verifier"],
        }, timeout=20)
    except httpx.HTTPError as e:
        raise GoogleError("Couldn't reach Google. Please try again.") from e
    if r.status_code != 200 or "id_token" not in r.json():
        raise GoogleError("Google didn't confirm the sign-in. Please try again.")
    c = _claims(r.json()["id_token"])
    if c.get("iss") not in ISSUERS or c.get("aud") != os.environ["GOOGLE_CLIENT_ID"]:
        raise GoogleError("This sign-in token wasn't issued for this office.")
    if int(c.get("exp", 0)) < time.time() - 60:
        raise GoogleError("The sign-in token has expired. Please try again.")
    if c.get("nonce") != row["nonce"]:
        raise GoogleError("Sign-in check failed. Please try again.")
    email = (c.get("email") or "").strip().lower()
    if not email or c.get("email_verified") not in (True, "true"):
        raise GoogleError("Your Google account's email isn't verified.")
    dom = allowed_domain()
    if dom and (c.get("hd") or "").lower() != dom:
        raise GoogleError(f"Use your @{dom} Google Workspace account.")

    return {"email": email, "sub": str(c.get("sub") or ""), "name": c.get("name") or email}
