"""Google sign-in with Google's token endpoint faked. Invite-only; every check on the ID token must hold."""
import base64
import json
import time
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from conftest import bearer


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@pytest.fixture
def g(api, monkeypatch):
    from app import google_auth
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("CABOT_PUBLIC_URL", "https://brain.firm.in")
    api.headers.pop("Authorization")
    api.token_claims = {}
    api.token_posts = []

    class R:
        status_code = 200
        def json(self): return {"id_token": jwt(api.token_claims)}

    monkeypatch.setattr(google_auth.httpx, "post", lambda url, data, timeout: api.token_posts.append(data) or R())
    return api


def begin(api, **claims):
    r = api.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    api.token_claims = {"iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com",
                        "exp": int(time.time()) + 300, "nonce": q["nonce"][0], "email": "arjun@x.in",
                        "email_verified": True, "sub": "g-123", "name": "Arjun"} | claims
    return q


def callback(api, q, **over):
    r = api.get("/api/auth/google/callback", params={"code": "c0de", "state": q["state"][0]} | over, follow_redirects=False)
    assert r.status_code == 302
    return r


def err(r):
    loc = r.headers["location"]
    return unquote(loc.split("auth_error=")[1]) if "auth_error=" in loc else None


def test_start_uses_pkce_and_exact_redirect(g):
    q = begin(g)
    assert q["redirect_uri"] == ["https://brain.firm.in/api/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    assert "cab_oauth" in g.cookies


def test_invited_member_signs_in_and_is_linked(g):
    q = begin(g)
    r = callback(g, q)
    assert err(r) is None and r.headers["location"] == "/#/today" and "cab_session" in r.cookies
    me = g.get("/api/bootstrap").json()["me"]
    assert me["email"] == "arjun@x.in" and me["google_linked"] and not me["must_change_pw"]
    assert g.token_posts[0]["code_verifier"] and g.token_posts[0]["redirect_uri"].startswith("https://brain.firm.in")
    # replaying the same state is refused
    assert "expired" in err(callback(g, q))


def test_uninvited_email_rejected(g):
    q = begin(g, email="stranger@gmail.com", sub="g-999")
    assert "isn't on this office's team" in err(callback(g, q))


@pytest.mark.parametrize("claims,msg", [
    ({"aud": "someone-else"}, "wasn't issued for this office"),
    ({"iss": "https://evil.example"}, "wasn't issued for this office"),
    ({"nonce": "forged"}, "check failed"),
    ({"email_verified": False}, "isn't verified"),
    ({"exp": int(time.time()) - 600}, "expired"),
])
def test_token_checks(g, claims, msg):
    q = begin(g)
    g.token_claims |= claims
    assert msg in err(callback(g, q))


def test_state_must_match_browser(g):
    q = begin(g)
    g.cookies.clear()  # callback arrives in a browser that didn't start the sign-in (login CSRF)
    assert "different browser" in err(callback(g, q))
    assert "expired" in err(callback(g, {"state": ["made-up"]}))


def test_workspace_domain_lock(g, monkeypatch):
    monkeypatch.setenv("GOOGLE_ALLOWED_DOMAIN", "x.in")
    q = begin(g)
    assert q["hd"] == ["x.in"]
    assert "@x.in" in err(callback(g, q))  # token without hd claim (personal Gmail) refused
    q = begin(g, hd="x.in")
    assert err(callback(g, q)) is None


def test_deactivated_and_relinked_accounts(g):
    q = begin(g)
    callback(g, q)
    g.cookies.clear()
    # a different Google account with the same email can't take over the linked member
    q = begin(g, sub="g-OTHER")
    assert "different Google account" in err(callback(g, q))
    partner = g.post("/api/auth/login", json={"email": "neha@x.in", "password": "partner-pass"}).json()["token"]
    g.patch(f"/api/users/{g.staff_id}", json={"active": False}, headers=bearer(partner))
    g.cookies.clear()
    q = begin(g)
    assert "turned off" in err(callback(g, q))


def test_cancel_and_not_configured(g, monkeypatch):
    assert "cancelled" in err(g.get("/api/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False))
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    assert g.get("/api/auth/google/start", follow_redirects=False).status_code == 404
    assert g.get("/api/auth/state").json()["google"] is False
