"""Authentication seam.

Everything the rest of the app knows about auth is in this module:
    login_required, current_user, and the routes /login, /login/google, /auth/callback, /logout.

Two providers share one contract (`start_login()`):
    DevProvider    AUTH_MODE=dev     one-click sign-in as a fixed local user. Config refuses it in production,
                                     and the class refuses to be built when settings.production is set.
    GoogleProvider AUTH_MODE=google  Google OpenID Connect (authorization-code flow with PKCE), restricted to one
                                     Google Workspace domain.

Google sign-in security checklist (each item has a test in tests/test_sso.py):
    * `state` (CSRF), `nonce` (replay) and PKCE (code interception) are generated per login and checked on return
    * the ID token's signature (Google's JWKS), issuer, audience and expiry are validated by Authlib
    * access is granted only if ALL of: email_verified, the `hd` (hosted domain) claim, and the email's own domain
      equal ALLOWED_DOMAIN. The `hd` request parameter is only a hint for Google's account chooser, never trusted
    * the session is cleared on login (no session fixation) and on logout
    * every failure sends the browser back to /login with a short, fixed message (nothing from the IdP is reflected)
"""
from __future__ import annotations

import logging
import secrets
from functools import wraps

from flask import jsonify, redirect, render_template, request, session, url_for

audit = logging.getLogger("audit")
log = logging.getLogger("auth")

LOGIN_ERRORS = {
    "failed": "Sign-in didn't complete. Please try again.",
    "domain": "That account isn't allowed to use this tool.",
    "expired": "Your session expired. Sign in again.",
    "toomany": "Too many sign-in attempts. Wait a minute and try again.",
}


def current_user() -> dict | None:
    u = session.get("user")
    return u if isinstance(u, dict) and u.get("email") else None


def actor() -> str:
    u = current_user()
    return u["email"] if u else "anonymous"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Session expired."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


class DevProvider:
    """One-click sign-in as a fixed local user. Development only."""
    name = "dev"

    def __init__(self, settings):
        if settings.production:   # second line of defence: config.py already refuses this combination
            raise RuntimeError("The development sign-in stub must never run in production.")
        self.user = {"name": settings.dev_user_name, "email": settings.dev_user_email}

    def start_login(self):
        session.clear()                       # new session on login (also rotates the CSRF token)
        session["user"] = dict(self.user)
        session.permanent = True
        audit.info("actor=%s event=login provider=dev", self.user["email"])
        return redirect(url_for("overview"))


class GoogleProvider:
    """Google OpenID Connect, restricted to one Workspace domain."""
    name = "google"

    def __init__(self, app, settings):
        from authlib.integrations.flask_client import OAuth

        self.settings = settings
        self.domain = settings.allowed_domain
        self.client = OAuth(app).register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url=settings.oidc_discovery_url,      # discovery document: endpoints, issuer, signing keys
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )

    def redirect_uri(self) -> str:
        # PUBLIC_BASE_URL (required in production) fixes the address; otherwise fall back to the request's own URL
        base = self.settings.public_base_url or request.url_root.rstrip("/")
        return base + url_for("auth_callback")

    def start_login(self):
        try:
            return self.client.authorize_redirect(
                self.redirect_uri(),
                nonce=secrets.token_urlsafe(24),
                hd=self.domain,                 # hint only: pre-filters Google's account chooser
                prompt="select_account",
            )
        except Exception:                      # e.g. discovery document unreachable
            log.exception("could not start the Google sign-in")
            audit.warning("event=login_failed provider=google reason=cannot_start")
            return redirect(url_for("login", error="failed"))

    def reject_reason(self, claims: dict) -> str | None:
        """Why this identity may not enter, or None if it may. Needs ALL of: verified email, Workspace domain, email domain."""
        email = str(claims.get("email", "")).strip().lower()
        local, _, host = email.rpartition("@")
        if not claims.get("sub"):
            return "no_subject"
        if not local or "@" in local or not host:
            return "no_valid_email"
        if claims.get("email_verified") not in (True, "true"):
            return "email_not_verified"
        if str(claims.get("hd", "")).strip().lower() != self.domain:
            return "wrong_workspace"
        if host != self.domain:
            return "wrong_email_domain"
        return None

    def finish_login(self):
        if request.args.get("error"):          # the user cancelled, or Google refused: nothing to validate
            audit.info("event=login_failed provider=google reason=idp_error:%s", request.args.get("error", "")[:40])
            return redirect(url_for("login", error="failed"))
        try:
            token = self.client.authorize_access_token()   # checks state, exchanges the code (PKCE), validates the ID token
        except Exception as e:                 # bad/replayed state, bad signature/nonce/audience/expiry, network...
            audit.warning("event=login_failed provider=google reason=%s", type(e).__name__)
            log.info("sign-in rejected: %s: %s", type(e).__name__, e)
            return redirect(url_for("login", error="failed"))

        claims = token.get("userinfo") or {}
        reason = self.reject_reason(claims)
        if reason:
            audit.warning("event=login_denied provider=google reason=%s email=%s", reason,
                          str(claims.get("email", ""))[:80].replace("\n", " "))
            return redirect(url_for("login", error="domain"))

        email = str(claims["email"]).strip().lower()
        session.clear()                        # new session on login: no session fixation; also rotates the CSRF token
        session["user"] = {"name": (str(claims.get("name") or "").strip() or email.split("@")[0])[:80], "email": email}
        session.permanent = True
        audit.info("actor=%s event=login provider=google", email)
        return redirect(url_for("overview"))


def register(app, settings, rate_limit=None) -> None:
    """Add the sign-in routes. `rate_limit` is a decorator applied to the anonymous sign-in endpoints."""
    rate_limit = rate_limit or (lambda view: view)
    provider = DevProvider(settings) if settings.auth_mode == "dev" else GoogleProvider(app, settings)

    @app.get("/login")
    def login():
        if current_user():
            return redirect(url_for("overview"))
        error = LOGIN_ERRORS.get(request.args.get("error", ""))
        return render_template("login.html", allowed_domain=settings.allowed_domain, error=error)

    @app.get("/login/google")
    @rate_limit
    def login_google():
        return provider.start_login()

    if provider.name == "google":
        @app.get("/auth/callback")
        @rate_limit
        def auth_callback():
            return provider.finish_login()

    @app.post("/logout")
    def logout():
        audit.info("actor=%s event=logout", actor())
        session.clear()
        return redirect(url_for("login"))
