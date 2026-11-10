"""Google SSO, tested end to end against a real (local) OpenID Connect provider: tests/fake_idp.py.

The app's own code path runs unchanged: discovery, authorize redirect (state, nonce, PKCE), code exchange, ID-token
signature/claim validation by Authlib, and our domain rules. Only the provider is local instead of Google.
"""
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from app import create_app
from aws_ops import FakeBackend
from auth import DevProvider
from config import ConfigError, Settings
from tests.fake_idp import GOOD_USER, FakeIdP

EXAMPLE = str(Path(__file__).resolve().parent.parent / "clients.example.json")
BASE = "http://panel.test"
DOMAIN = "life180labs.com"


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.setenv("AUTHLIB_INSECURE_TRANSPORT", "1")          # the stand-in provider is plain http on localhost
    provider = FakeIdP()
    yield provider
    provider.close()


def make_app(idp, **over):
    env = {"AUTH_MODE": "google", "AWS_MODE": "fake", "CLIENTS_FILE": EXAMPLE, "SECRET_KEY": "test-secret",
           "RATE_LIMIT_ENABLED": "0", "FAKE_SPEED": "0", "STATUS_CACHE_TTL": "0",
           "GOOGLE_CLIENT_ID": idp.client_id, "GOOGLE_CLIENT_SECRET": idp.client_secret,
           "OIDC_DISCOVERY_URL": idp.discovery_url, "PUBLIC_BASE_URL": BASE, "ALLOWED_DOMAIN": DOMAIN, **over}
    settings = Settings.from_env(env)
    return create_app(settings, FakeBackend(settings.clients, speed=0))


@pytest.fixture
def app(idp):
    return make_app(idp)


@pytest.fixture
def client(app):
    return app.test_client()


def authorize_url(c, idp):
    r = c.get("/login/google")
    assert r.status_code == 302, r.get_data(as_text=True)
    loc = r.headers["Location"]
    assert loc.startswith(idp.base + "/authorize"), loc
    return loc


def sign_in(c, idp):
    """Walk the whole flow like a browser: app -> provider -> app. Returns (callback response, callback path+query)."""
    back = requests.get(authorize_url(c, idp), allow_redirects=False, timeout=5)
    callback = back.headers["Location"]
    assert callback.startswith(BASE + "/auth/callback"), callback
    path = callback[len(BASE):]
    return c.get(path), path


def assert_signed_in(c):
    assert c.get("/api/overview").status_code == 200


def assert_signed_out(c):
    assert c.get("/api/overview").status_code == 401


# ---------------------------------------------------------------- the redirect to the provider

def test_sign_in_page_links_to_google_and_names_the_domain(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'href="/login/google"' in html and "Sign in with Google" in html and DOMAIN in html


def test_redirect_carries_state_nonce_pkce_and_domain_hint(client, idp):
    q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url(client, idp)).query).items()}
    assert q["client_id"] == idp.client_id and q["response_type"] == "code"
    assert q["redirect_uri"] == BASE + "/auth/callback"           # fixed by PUBLIC_BASE_URL, not by request headers
    assert set(q["scope"].split()) == {"openid", "email", "profile"}
    assert len(q["state"]) >= 16 and len(q["nonce"]) >= 16        # unguessable
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 40
    assert q["hd"] == DOMAIN and q["prompt"] == "select_account"  # hints only; enforcement happens on the way back


def test_each_attempt_gets_fresh_state_and_nonce(client, idp):
    a = parse_qs(urlparse(authorize_url(client, idp)).query)
    b = parse_qs(urlparse(authorize_url(client, idp)).query)
    assert a["state"] != b["state"] and a["nonce"] != b["nonce"] and a["code_challenge"] != b["code_challenge"]


# ---------------------------------------------------------------- success

def test_valid_workspace_user_signs_in(client, idp):
    resp, _ = sign_in(client, idp)
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert_signed_in(client)
    page = client.get("/").get_data(as_text=True)
    assert "Ada Lovelace" in page and "ada@life180labs.com" in page
    token_request = idp.token_requests[0]
    assert token_request["grant_type"] == "authorization_code" and len(token_request["code_verifier"]) >= 43   # PKCE used


def test_email_is_lowercased_and_missing_name_falls_back_to_the_local_part(client, idp):
    idp.claim_overrides = {"email": "Grace.Hopper@LIFE180LABS.COM", "hd": "LIFE180LABS.COM", "name": None}
    sign_in(client, idp)
    with client.session_transaction() as s:
        assert s["user"] == {"name": "grace.hopper", "email": "grace.hopper@life180labs.com"}


def test_email_verified_may_arrive_as_the_string_true(client, idp):
    idp.claim_overrides = {"email_verified": "true"}
    sign_in(client, idp)
    assert_signed_in(client)


def test_signed_in_user_is_sent_away_from_the_login_page_and_can_sign_out(client, idp):
    sign_in(client, idp)
    assert client.get("/login").status_code == 302
    html = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = client.post("/logout", data={"csrf_token": token})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert_signed_out(client)
    assert client.get("/").status_code == 302


# ---------------------------------------------------------------- who is allowed in (domain rules)

DENIED = [
    pytest.param({"email": "someone@gmail.com", "hd": None}, id="personal-gmail"),
    pytest.param({"email": "x@other.com", "hd": "other.com"}, id="other-workspace"),
    pytest.param({"email_verified": False}, id="email-not-verified"),
    pytest.param({"email_verified": "false"}, id="email-verified-string-false"),
    pytest.param({"email_verified": "yes"}, id="email-verified-odd-string"),
    pytest.param({"email_verified": None}, id="email-verified-missing"),
    pytest.param({"email": "x@evil.com"}, id="right-hd-but-foreign-email"),
    pytest.param({"hd": None}, id="right-email-but-no-hd"),
    pytest.param({"hd": "other.com"}, id="right-email-but-other-hd"),
    pytest.param({"email": "x@life180labs.com.evil.com"}, id="suffix-lookalike"),
    pytest.param({"email": "x@evil-life180labs.com"}, id="prefix-lookalike"),
    pytest.param({"email": "a@evil.com@life180labs.com"}, id="double-at-sign"),
    pytest.param({"email": "x@eng.life180labs.com"}, id="subdomain-is-not-the-domain"),
    pytest.param({"email": "@life180labs.com"}, id="empty-local-part"),
    pytest.param({"email": "life180labs.com"}, id="no-at-sign"),
    pytest.param({"email": None}, id="no-email-claim"),
]


@pytest.mark.parametrize("overrides", DENIED)
def test_identities_outside_the_company_are_refused(client, idp, overrides):
    idp.claim_overrides = overrides
    resp, _ = sign_in(client, idp)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login?error=domain")
    assert_signed_out(client)
    with client.session_transaction() as s:
        assert "user" not in s


def test_refusal_shows_a_fixed_friendly_message_and_reflects_nothing_from_the_provider(client, idp):
    idp.claim_overrides = {"email": "<script>alert(1)</script>@gmail.com", "hd": None, "name": "<img src=x onerror=alert(1)>"}
    resp, _ = sign_in(client, idp)
    html = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "isn&#39;t allowed to use this tool" in html
    assert "<script>alert" not in html and "onerror" not in html and "gmail" not in html


# ---------------------------------------------------------------- forged or invalid tokens

BAD_TOKENS = [
    pytest.param({"aud": "some-other-client"}, id="wrong-audience"),
    pytest.param({"iss": "https://evil.example.com"}, id="wrong-issuer"),
    pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
    pytest.param({"nonce": "not-the-nonce-we-sent"}, id="wrong-nonce"),
    pytest.param({"nonce": None}, id="missing-nonce"),
    pytest.param({"sub": None}, id="missing-subject"),     # Authlib refuses it before our own domain rules run
]


@pytest.mark.parametrize("overrides", BAD_TOKENS)
def test_invalid_id_tokens_are_rejected(client, idp, overrides):
    idp.claim_overrides = overrides
    resp, _ = sign_in(client, idp)
    assert resp.headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)


def test_token_signed_with_a_key_the_provider_did_not_publish_is_rejected(client, idp):
    idp.forge_signature = True                      # same key id, different key: only the signature check can catch it
    resp, _ = sign_in(client, idp)
    assert resp.headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)


# ---------------------------------------------------------------- state, replay, interruption

def test_tampered_state_from_the_provider_is_rejected(client, idp):
    idp.tamper_state = True
    resp, _ = sign_in(client, idp)
    assert resp.headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)
    assert idp.token_requests == []                 # we never even exchanged the code


def test_callback_with_edited_state_is_rejected(client, idp):
    back = requests.get(authorize_url(client, idp), allow_redirects=False, timeout=5).headers["Location"]
    path = re.sub(r"state=[^&]+", "state=forged", back[len(BASE):])
    assert client.get(path).headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)


@pytest.mark.parametrize("query", ["", "?code=abc", "?state=abc", "?code=abc&state=abc"])
def test_callback_without_a_started_login_is_rejected(client, query):
    assert client.get("/auth/callback" + query).headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)


def test_callback_cannot_be_replayed_in_the_same_browser(client, idp):
    resp, path = sign_in(client, idp)
    assert resp.headers["Location"] == "/"
    assert client.get(path).headers["Location"].endswith("/login?error=failed")     # state was consumed


def test_callback_cannot_be_replayed_from_another_browser(client, app, idp):
    victim_path = sign_in(client, idp)[1]                                           # a callback URL that was captured and used
    attacker = app.test_client()
    authorize_url(attacker, idp)                                                    # the attacker has their own pending login
    assert attacker.get(victim_path).headers["Location"].endswith("/login?error=failed")
    assert_signed_out(attacker)


def test_authorization_code_works_only_once(client, idp):
    back = requests.get(authorize_url(client, idp), allow_redirects=False, timeout=5).headers["Location"]
    first_code = parse_qs(urlparse(back).query)["code"][0]
    assert client.get(back[len(BASE):]).headers["Location"] == "/"                  # first use succeeds
    other = client.application.test_client()                                        # new login (fresh state), old code
    fresh = requests.get(authorize_url(other, idp), allow_redirects=False, timeout=5).headers["Location"]
    replay = re.sub(r"code=[^&]+", "code=" + first_code, fresh[len(BASE):])
    assert other.get(replay).headers["Location"].endswith("/login?error=failed")
    assert_signed_out(other)


def test_user_cancelling_at_the_provider_is_handled_gracefully(client, idp):
    idp.authorize_error = "access_denied"
    resp, _ = sign_in(client, idp)
    assert resp.headers["Location"].endswith("/login?error=failed")
    assert idp.token_requests == []
    assert_signed_out(client)


def test_provider_unreachable_when_starting_sign_in_does_not_crash(idp):
    app = make_app(idp, OIDC_DISCOVERY_URL="http://127.0.0.1:9/.well-known/openid-configuration")
    r = app.test_client().get("/login/google")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?error=failed")


def test_provider_going_down_before_the_callback_does_not_crash(client, idp):
    back = requests.get(authorize_url(client, idp), allow_redirects=False, timeout=5).headers["Location"]
    idp.close()
    r = client.get(back[len(BASE):])
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?error=failed")
    assert_signed_out(client)


def test_callback_only_accepts_get(client):
    assert client.post("/auth/callback").status_code in (400, 404, 405)


# ---------------------------------------------------------------- session safety

def test_session_is_replaced_on_login_no_session_fixation(client, idp):
    with client.session_transaction() as s:
        s["planted"] = "attacker-value"
    sign_in(client, idp)
    with client.session_transaction() as s:
        assert "planted" not in s and s["user"]["email"] == "ada@life180labs.com"
        assert not any(k.startswith("_state_google") for k in s)                    # login state is cleaned up


def test_csrf_token_rotates_on_login(client, idp):
    def csrf_token():
        return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)

    sign_in(client, idp)
    before = csrf_token()
    client.post("/logout", data={"csrf_token": before})
    sign_in(client, idp)
    assert csrf_token() != before


def test_session_cookie_flags(client, idp):
    r, _ = sign_in(client, idp)
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie        # Lax: the cookie must survive the return from Google


# ---------------------------------------------------------------- operational behaviour

def test_sign_in_endpoints_are_rate_limited_with_a_friendly_page(idp):
    app = make_app(idp, RATE_LIMIT_ENABLED="1", RATE_LIMIT_LOGIN="2 per minute")
    c = app.test_client()
    codes = [c.get("/login/google").status_code for _ in range(2)]
    assert codes == [302, 302]
    r = c.get("/login/google")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?error=toomany")
    assert "Too many sign-in attempts" in c.get(r.headers["Location"]).get_data(as_text=True)
    assert c.get("/healthz").status_code == 200


def test_audit_log_records_outcomes_but_never_secrets(client, idp, caplog):
    caplog.set_level(logging.INFO)
    resp, path = sign_in(client, idp)
    code = parse_qs(urlparse(path).query)["code"][0]
    idp.claim_overrides = {"email": "x@gmail.com", "hd": None}
    denied_client = client.application.test_client()
    sign_in(denied_client, idp)
    text = caplog.text
    assert "event=login provider=google" in text and "ada@life180labs.com" in text
    assert "event=login_denied provider=google reason=wrong_workspace email=x@gmail.com" in text
    assert code not in text and "test-client-secret" not in text and "id_token" not in text


def test_pages_and_api_require_sign_in(client):
    assert client.get("/").status_code == 302 and client.get("/c/uniservice").status_code == 302
    assert client.get("/api/overview").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_google_mode_never_signs_anyone_in_without_the_provider(client):
    client.get("/login/google")
    assert_signed_out(client)


# ---------------------------------------------------------------- provider selection and config

def test_dev_mode_has_no_callback_route_and_google_mode_has_no_auto_login(idp):
    dev = create_app(Settings.from_env({"AUTH_MODE": "dev", "AWS_MODE": "fake", "CLIENTS_FILE": EXAMPLE, "RATE_LIMIT_ENABLED": "0"}))
    assert dev.test_client().get("/auth/callback").status_code == 404
    c = make_app(idp).test_client()
    assert c.get("/login/google").headers["Location"].startswith(idp.base)           # goes to the provider, not "/"


def test_dev_provider_refuses_to_exist_in_production():
    with pytest.raises(RuntimeError, match="never run in production"):
        DevProvider(SimpleNamespace(production=True, dev_user_name="x", dev_user_email="x@example.com"))


GOOD = {"AUTH_MODE": "google", "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret", "CLIENTS_FILE": EXAMPLE}
PROD = {**GOOD, "RAILWAY_ENVIRONMENT": "production", "SECRET_KEY": "k", "PUBLIC_BASE_URL": "https://panel.example.com"}


@pytest.mark.parametrize("env,frag", [
    ({**GOOD, "GOOGLE_CLIENT_ID": ""}, "GOOGLE_CLIENT_ID"),
    ({**GOOD, "GOOGLE_CLIENT_SECRET": ""}, "GOOGLE_CLIENT_SECRET"),
    ({**GOOD, "ALLOWED_DOMAIN": "@life180labs.com"}, "ALLOWED_DOMAIN"),
    ({**GOOD, "ALLOWED_DOMAIN": "localhost"}, "ALLOWED_DOMAIN"),
    ({**GOOD, "ALLOWED_DOMAIN": "a b.com"}, "ALLOWED_DOMAIN"),
    ({**GOOD, "PUBLIC_BASE_URL": "panel.example.com"}, "PUBLIC_BASE_URL"),
    ({**GOOD, "PUBLIC_BASE_URL": "https://panel.example.com/app"}, "PUBLIC_BASE_URL"),
    ({k: v for k, v in PROD.items() if k != "PUBLIC_BASE_URL"}, "PUBLIC_BASE_URL"),
    ({**PROD, "PUBLIC_BASE_URL": "http://panel.example.com"}, "PUBLIC_BASE_URL"),
    ({**PROD, "OIDC_DISCOVERY_URL": "http://idp.example.com/.well-known/openid-configuration"}, "OIDC_DISCOVERY_URL"),
    ({k: v for k, v in PROD.items() if k != "SECRET_KEY"}, "SECRET_KEY"),
    ({**PROD, "AUTH_MODE": "dev"}, "AUTH_MODE=dev"),
    ({**PROD, "AWS_MODE": "fake"}, "AWS_MODE=fake"),
])
def test_unsafe_sso_configuration_refuses_to_start(env, frag):
    with pytest.raises(ConfigError, match=frag):
        Settings.from_env(env)


def test_a_complete_production_configuration_starts_and_defaults_to_google():
    s = Settings.from_env({k: v for k, v in PROD.items() if k != "AUTH_MODE"})       # production defaults to google
    assert (s.auth_mode, s.production, s.allowed_domain) == ("google", True, DOMAIN)
    assert s.oidc_discovery_url == "https://accounts.google.com/.well-known/openid-configuration"
    assert s.public_base_url == "https://panel.example.com"


def test_allowed_domain_is_normalised_to_lowercase():
    assert Settings.from_env({**GOOD, "ALLOWED_DOMAIN": " LIFE180LABS.com "}).allowed_domain == DOMAIN


def test_good_user_fixture_matches_the_default_domain():
    assert GOOD_USER["email"].endswith("@" + DOMAIN) and GOOD_USER["hd"] == DOMAIN
