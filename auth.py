"""Authentication seam.

Everything the rest of the app knows about auth is in this module:
    login_required, current_user, and the routes /login, /login/google, /logout.

Phase 1 ships a development stub (AUTH_MODE=dev). Phase 2 adds Google SSO as a second provider with the
same `start_login()` contract; nothing outside this file should need to change. Config refuses to start
the stub in production (see config.Settings.from_env).
"""
from __future__ import annotations

import logging
from functools import wraps

from flask import jsonify, redirect, render_template, request, session, url_for

audit = logging.getLogger("audit")

LOGIN_ERRORS = {
    "failed": "Sign-in didn't complete. Please try again.",
    "domain": "That account isn't allowed to use this tool.",
    "expired": "Your session expired. Sign in again.",
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
        self.user = {"name": settings.dev_user_name, "email": settings.dev_user_email}

    def start_login(self):
        session.clear()                       # new session on login (also rotates the CSRF token)
        session["user"] = dict(self.user)
        session.permanent = True
        audit.info("actor=%s event=login provider=dev", self.user["email"])
        return redirect(url_for("overview"))


def register(app, settings) -> None:
    provider = DevProvider(settings)          # Phase 2: choose GoogleProvider when AUTH_MODE=google

    @app.get("/login")
    def login():
        if current_user():
            return redirect(url_for("overview"))
        error = LOGIN_ERRORS.get(request.args.get("error", ""))
        return render_template("login.html", allowed_domain=settings.allowed_domain, error=error)

    @app.get("/login/google")
    def login_google():
        return provider.start_login()

    @app.post("/logout")
    def logout():
        audit.info("actor=%s event=logout", actor())
        session.clear()
        return redirect(url_for("login"))
