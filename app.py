"""AWS Environment Control: Flask application factory.

Run locally:   AWS_MODE=fake python app.py          (see README.md)
Production:    gunicorn "app:create_app()"          (see Procfile)
"""
from __future__ import annotations

import logging
import mimetypes
import sys
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter, RateLimitExceeded
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import api
import auth
from aws_ops import FakeBackend, RealBackend, ServiceError
from config import DESIGN_DIR, ConfigError, Settings
from operations import EnvironmentService

CSP = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": ["'self'", "'unsafe-inline'"],   # _icons.html uses a style attribute on the sprite
    "img-src": ["'self'", "data:"],
    "font-src": "'self'",
    "connect-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
    "frame-ancestors": "'none'",
}


def _setup_logging():
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None, backend=None) -> Flask:
    settings = settings or Settings.from_env()
    _setup_logging()
    mimetypes.add_type("font/woff2", ".woff2")     # not in every OS registry; the UI's fonts depend on it

    app = Flask(__name__, template_folder=str(DESIGN_DIR / "templates"), static_folder=str(DESIGN_DIR / "static"))
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_NAME="l180_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.production,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_hours),
        WTF_CSRF_TIME_LIMIT=None,                      # token lives as long as the session
        WTF_CSRF_HEADERS=["X-CSRF-Token", "X-CSRFToken"],
        MAX_CONTENT_LENGTH=64 * 1024,
        RATELIMIT_ENABLED=settings.rate_limit_enabled,
        RATELIMIT_HEADERS_ENABLED=True,
    )
    if settings.trust_proxy:                           # Railway terminates TLS in front of the app
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    if backend is None:
        backend = FakeBackend(settings.clients, speed=settings.fake_speed) if settings.aws_mode == "fake" else RealBackend()
    service = EnvironmentService(settings.clients, backend, cache_ttl=settings.cache_ttl,
                                 poll_interval=settings.poll_interval, wait_timeout=settings.wait_timeout,
                                 done_retention=settings.done_retention, failed_retention=settings.failed_retention)
    app.extensions["l180_service"] = service
    app.extensions["l180_settings"] = settings

    CSRFProtect(app)
    talisman = Talisman(
        app, force_https=settings.production, strict_transport_security=settings.production,
        session_cookie_secure=settings.production, content_security_policy=CSP,
        frame_options="DENY", referrer_policy="same-origin",
    )

    def rate_key():
        u = session.get("user") or {}
        return u.get("email") or get_remote_address()
    limiter = Limiter(rate_key, app=app, storage_uri="memory://", default_limits=[])
    limiter.limit(settings.rate_read, methods=["GET"])(api.bp)
    limiter.limit(settings.rate_write, methods=["POST"])(api.bp)

    auth.register(app, settings)
    app.register_blueprint(api.bp)

    # ---------- pages ----------

    def shell_ctx(**extra):
        return dict(user=auth.current_user(), clients=[c.public() for c in settings.clients], **extra)

    @app.get("/")
    @auth.login_required
    def overview():
        return render_template("overview.html", **shell_ctx())

    @app.get("/c/<slug>")
    @auth.login_required
    def client_page(slug):
        client = service.clients.get(slug)
        if not client:
            return not_found_page()
        return render_template("dashboard.html", **shell_ctx(client=client.public()))

    @app.get("/healthz")
    @talisman(force_https=False)                       # Railway probes over plain HTTP
    def healthz():
        return jsonify(ok=True)

    # ---------- errors: JSON for /api, friendly pages elsewhere ----------

    def wants_json():
        return request.path.startswith("/api/")

    def not_found_page():
        return ("<!doctype html><title>Not found</title><body style='font:16px system-ui;padding:48px'>"
                f"<h1>Not found</h1><p><a href='{url_for('overview')}'>Back to All Clients</a></p>"), 404

    @app.errorhandler(ServiceError)
    def on_service_error(e):
        return jsonify(ok=False, error=e.message), e.status

    @app.errorhandler(CSRFError)
    def on_csrf(e):
        if wants_json():
            return jsonify(ok=False, error="Your session expired. Reload the page and try again."), 400
        return redirect(url_for("login", error="expired"))

    @app.errorhandler(RateLimitExceeded)
    def on_rate_limit(e):
        return jsonify(ok=False, error="Too many requests. Wait a moment and try again."), 429

    @app.errorhandler(HTTPException)
    def on_http_error(e):
        if wants_json():
            messages = {404: "Not found.", 405: "Not found.", 413: "Request too large."}
            return jsonify(ok=False, error=messages.get(e.code, e.description)), (404 if e.code == 405 else e.code)
        if e.code == 404:
            return not_found_page()
        return e

    @app.errorhandler(Exception)
    def on_crash(e):
        app.logger.exception("unhandled error")
        if wants_json():
            return jsonify(ok=False, error="Something went wrong. Try again."), 500
        return "Something went wrong.", 500

    @app.after_request
    def no_store(resp):
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


if __name__ == "__main__":
    import os
    try:
        create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
    except ConfigError as err:
        sys.exit(f"Configuration error: {err}")
