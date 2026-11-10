import re
from pathlib import Path

import pytest

from app import create_app
from aws_ops import FakeBackend, ServiceError
from config import Settings

EXAMPLE = str(Path(__file__).resolve().parent.parent / "clients.example.json")   # never the local clients.json
ENV = {"CLIENTS_FILE": EXAMPLE, "AWS_MODE": "fake", "STATUS_CACHE_TTL": "0", "OP_POLL_INTERVAL": "0.01", "FAKE_SPEED": "0",
       "RATE_LIMIT_ENABLED": "0", "SECRET_KEY": "test-secret"}
# example clients: "uniservice" (2 DB + 2 app, running) and "another-client" (1 app, stopped, no DB)
UNI_APP, UNI_APP2, UNI_DB = "i-0123456789abcdef0", "i-0fedcba9876543210", "uniservice-db"
OTHER_APP = "i-0aaaabbbbccccdddd"


def build(**over):
    env = {**ENV, **{k: str(v) for k, v in over.items()}}
    settings = Settings.from_env(env)
    backend = FakeBackend(settings.clients, speed=settings.fake_speed)
    app = create_app(settings, backend)
    app.testing = False   # keep real error handlers
    return app, backend


@pytest.fixture
def app_backend():
    return build()


@pytest.fixture
def client(app_backend):
    return app_backend[0].test_client()


def sign_in(c):
    r = c.get("/login/google")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def csrf(c, path="/"):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1) if 'name="csrf-token"' in html else \
        re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def authed(client):
    sign_in(client)
    return client, {"X-CSRF-Token": csrf(client)}


def svc(app):
    return app.extensions["l180_service"]


# ---------- auth ----------

def test_healthz_is_public_and_plain_http_safe(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json == {"ok": True}


def test_pages_redirect_to_login_when_signed_out(client):
    for path in ("/", "/c/uniservice"):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/login")


def test_api_returns_401_json_when_signed_out(client):
    for method, path in (("get", "/api/overview"), ("get", "/api/c/uniservice/status"),
                         ("post", "/api/c/uniservice/environment/stop")):
        r = getattr(client, method)(path)
        assert r.status_code in (401, 400)            # POST is checked for CSRF first
    r = client.get("/api/overview")
    assert r.status_code == 401 and r.json == {"ok": False, "error": "Session expired."}


def test_login_page_renders_and_dev_login_flow(client):
    r = client.get("/login")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Sign in with Google" in html and "life180labs.com" in html
    sign_in(client)
    assert client.get("/login").status_code == 302         # already signed in -> overview
    page = client.get("/").get_data(as_text=True)
    assert "All Clients" in page and "Local Admin" in page and "admin@example.com" in page
    assert "Uniservice" in page and "Another Client" in page


def test_login_error_messages_are_mapped_not_reflected(client):
    html = client.get("/login?error=domain").get_data(as_text=True)
    assert "isn&#39;t allowed" in html
    html = client.get("/login?error=<script>alert(1)</script>").get_data(as_text=True)
    assert "<script>alert" not in html and 'id="login-alert"' not in html


def test_logout_requires_csrf_and_clears_session(authed):
    c, h = authed
    token = h["X-CSRF-Token"]
    r = c.post("/logout")                                   # no token
    assert r.status_code == 302 and "error=expired" in r.headers["Location"]
    assert c.get("/api/overview").status_code == 200        # still signed in
    r = c.post("/logout", data={"csrf_token": token})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert c.get("/api/overview").status_code == 401


def test_session_is_rotated_on_login(client):
    client.get("/login")
    sign_in(client)
    t1 = csrf(client)
    client.post("/logout", data={"csrf_token": t1})
    sign_in(client)
    assert csrf(client) != t1


# ---------- pages ----------

def test_dashboard_page_renders_client_and_sidebar_nav(authed):
    c, _ = authed
    html = c.get("/c/uniservice").get_data(as_text=True)
    assert 'data-client="uniservice"' in html and "AWS Environment Control" in html
    nav = re.search(r'<nav class="side-nav".*?</nav>', html, re.S).group(0)
    assert 'class="nav-item nav-all" href="/"' in nav and 'aria-current="page"' in nav
    assert nav.index('data-slug="uniservice"') < nav.index('data-slug="another-client"')          # configured order
    assert re.search(r'data-slug="uniservice" aria-current="page"', nav)                           # current client highlighted
    assert 'data-slug="another-client" aria-current' not in nav
    assert 'aria-expanded="true"' in nav and 'aria-controls="nav-clients"' in nav                   # expandable group
    assert "Another Client" in nav and "Sign out" in html and "admin@example.com" in html           # footer: user + sign out
    assert c.get("/c/nope").status_code == 404
    assert "Not found" in c.get("/c/nope").get_data(as_text=True)


def test_static_assets_and_fonts_are_served(client):
    for path, ctype in (("/static/css/style.css", "text/css"), ("/static/js/common.js", "javascript"),
                        ("/static/js/dashboard.js", "javascript"), ("/static/js/overview.js", "javascript"),
                        ("/static/js/login.js", "javascript"), ("/static/img/l180-logo.svg", "svg"),
                        ("/static/vendor/bootstrap.min.css", "text/css"),
                        ("/static/fonts/PlusJakartaSans-Bold.woff2", "font")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert ctype in r.content_type, (path, r.content_type)
    assert "no-store" not in client.get("/static/css/style.css").headers.get("Cache-Control", "")


# ---------- read API ----------

def test_overview_and_status_contract(authed):
    c, _ = authed
    ov = c.get("/api/overview").json
    assert [x["slug"] for x in ov["clients"]] == ["uniservice", "another-client"]
    uni = ov["clients"][0]
    assert uni["rds"] == [{"state": "available", "error": None}] * 2 and uni["operation"] is None
    assert ov["clients"][1]["rds"] == [] and ov["clients"][1]["ec2"][0]["state"] == "stopped"

    st = c.get("/api/c/uniservice/status").json
    assert st["client"] == {"slug": "uniservice", "name": "Uniservice"} and st["region"] == "ap-south-1"
    assert st["rds"][0] == {"id": UNI_DB, "name": "Primary database", "state": "available", "error": None}
    assert [r["name"] for r in st["ec2"]] == ["Web server", "API server"]


def test_status_unknown_client_404_json(authed):
    c, _ = authed
    r = c.get("/api/c/nope/status")
    assert r.status_code == 404 and r.json == {"ok": False, "error": "Unknown client."}


# ---------- write API ----------

def test_post_requires_csrf_header(authed):
    c, _ = authed
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop")
    assert r.status_code == 400 and r.json["ok"] is False
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop", headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 400
    assert c.get("/api/c/uniservice/status").json["ec2"][0]["state"] == "running"   # nothing happened


def test_single_resource_stop_start_roundtrip(authed):
    c, h = authed
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop", headers=h)
    assert r.status_code == 202 and r.json == {"ok": True, "message": "Stopping Web server."}
    assert c.get("/api/c/uniservice/status").json["ec2"][0]["state"] == "stopped"   # FAKE_SPEED=0
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop", headers=h)
    assert r.status_code == 409 and r.json["error"] == "Web server is already stopped."
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/start", headers=h)
    assert r.status_code == 202 and r.json["message"] == "Starting Web server."


def test_kind_level_actions(authed):
    c, h = authed
    r = c.post("/api/c/uniservice/rds/stop", headers=h)
    assert r.status_code == 202 and r.json["message"] == "Stopping 2 databases."
    r = c.post("/api/c/uniservice/rds/stop", headers=h)
    assert r.status_code == 409 and r.json["error"] == "Every database is already stopped."
    assert c.post("/api/c/another-client/rds/start", headers=h).status_code == 404       # client has no DBs


def test_unknown_resource_and_bad_routes_are_json_404(authed):
    c, h = authed
    r = c.post("/api/c/uniservice/ec2/i-0000000000000000f/stop", headers=h)
    assert r.status_code == 404 and r.json["error"] == "That resource is not set up for this client."
    assert c.post(f"/api/c/uniservice/ec2/{UNI_DB}/stop", headers=h).status_code == 404   # id from the wrong kind
    assert c.post("/api/c/uniservice/s3/stop", headers=h).json["ok"] is False
    assert c.post("/api/c/nope/environment/start", headers=h).status_code == 404
    r = c.get("/api/c/uniservice/environment/start")                                       # wrong method
    assert r.status_code == 404 and r.json["ok"] is False
    assert c.get("/api/does-not-exist").status_code == 404


def test_path_traversal_and_injection_ids_are_rejected(authed):
    c, h = authed
    for rid in ("..%2f..%2fetc", "i-0123456789abcdef0%3Bdrop", "%00", "a" * 300):
        r = c.post(f"/api/c/uniservice/ec2/{rid}/stop", headers=h)
        assert r.status_code == 404 and r.json["ok"] is False


def test_aws_denied_surfaces_message_and_status(app_backend):
    app, backend = app_backend
    c = app.test_client(); sign_in(c); h = {"X-CSRF-Token": csrf(c)}
    backend.fail_next = ServiceError("The dashboard isn't allowed to perform this action in AWS. Check the IAM policy.", 403)
    r = c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop", headers=h)
    assert r.status_code == 403 and "IAM policy" in r.json["error"]


def test_environment_start_stop_end_to_end(app_backend):
    app, backend = app_backend
    c = app.test_client(); sign_in(c); h = {"X-CSRF-Token": csrf(c)}
    r = c.post("/api/c/another-client/environment/start", headers=h)
    assert r.status_code == 202 and r.json["message"] == "Starting the applications."
    svc(app).join()
    st = c.get("/api/c/another-client/status").json
    assert st["operation"]["status"] == "done" and st["operation"]["steps"] == ["Start applications"]
    assert st["ec2"][0]["state"] == "running"
    assert c.post("/api/c/another-client/environment/start", headers=h).status_code == 409   # already running

    r = c.post("/api/c/uniservice/environment/stop", headers=h)
    assert r.status_code == 202 and r.json["message"] == "Stopping the applications."
    svc(app).join()
    st = c.get("/api/c/uniservice/status").json
    assert st["operation"]["steps"] == ["Stop applications", "Wait until stopped", "Stop databases"]
    assert st["operation"]["status"] == "done"
    assert {r["state"] for r in st["rds"] + st["ec2"]} == {"stopped"}
    ov = c.get("/api/overview").json["clients"][0]
    assert ov["operation"]["status"] == "done"


def test_conflict_while_operation_runs(app_backend):
    app, backend = app_backend
    c = app.test_client(); sign_in(c); h = {"X-CSRF-Token": csrf(c)}
    s = svc(app)
    s.poll_interval = 0.3                      # keep the operation alive long enough to probe it
    backend._speed = 1
    backend._rec[("uniservice", "ec2", UNI_APP)]  # exists
    r = c.post("/api/c/uniservice/environment/stop", headers=h)
    assert r.status_code == 202
    r = c.post("/api/c/uniservice/environment/stop", headers=h)
    assert r.status_code == 409 and "already in progress" in r.json["error"]
    r = c.post(f"/api/c/uniservice/rds/{UNI_DB}/stop", headers=h)
    assert r.status_code == 409


# ---------- security ----------

def test_security_headers_and_cookie_flags(client):
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"
    sign_in(client)
    cookie = [v for k, v in client.get("/").headers if k == "Set-Cookie"] or ["(none)"]
    r = client.get("/login/google")  # sets session cookie
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie   # dev: http


def test_no_inline_scripts_in_rendered_pages(authed):
    c, _ = authed
    for path in ("/", "/c/uniservice", "/login"):
        html = c.get(path).get_data(as_text=True)
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), path
        assert not re.search(r"\son(click|load|error)=", html), path


def test_rate_limit_returns_429_json():
    app, _ = build(RATE_LIMIT_ENABLED=1, RATE_LIMIT_READ="3 per minute")
    c = app.test_client(); sign_in(c)
    codes = [c.get("/api/overview").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    r = c.get("/api/overview")
    assert r.json == {"ok": False, "error": "Too many requests. Wait a moment and try again."}
    assert c.get("/healthz").status_code == 200                 # health checks are never limited


def test_write_rate_limit(app_backend):
    app, _ = build(RATE_LIMIT_ENABLED=1, RATE_LIMIT_WRITE="2 per minute")
    c = app.test_client(); sign_in(c); h = {"X-CSRF-Token": csrf(c)}
    codes = [c.post(f"/api/c/uniservice/ec2/{UNI_APP}/stop", headers=h).status_code for _ in range(4)]
    assert codes == [202, 409, 429, 429]


def test_dev_stub_cannot_run_in_production():
    from config import ConfigError
    with pytest.raises(ConfigError):
        Settings.from_env({"RAILWAY_ENVIRONMENT": "production", "AUTH_MODE": "dev", "SECRET_KEY": "x"})


# ---------- production behaviour (the real production configuration: Google sign-in, https address) ----------

PROD_ENV = {"RAILWAY_ENVIRONMENT": "production", "SECRET_KEY": "p" * 40, "CLIENTS_FILE": EXAMPLE,
            "GOOGLE_CLIENT_ID": "dummy.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "dummy-secret",
            "PUBLIC_BASE_URL": "https://panel.example.com", "ALLOWED_DOMAIN": "life180labs.com"}


def prod_app():
    """What gunicorn "app:create_app()" builds on Railway. No network is touched until someone actually signs in."""
    return create_app(Settings.from_env(PROD_ENV))


def test_production_uses_google_sign_in_and_has_no_dev_login():
    app = prod_app()
    assert app.extensions["l180_settings"].auth_mode == "google"
    c = app.test_client()
    https = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "panel.example.com"}
    stray = c.get("/auth/callback", headers=https)                    # the Google callback exists and rejects strays
    assert stray.status_code == 302 and stray.headers["Location"].endswith("/login?error=failed")
    assert c.get("/c/uniservice", headers=https).headers["Location"].endswith("/login")   # nobody is signed in by default


def test_production_redirects_http_to_https_but_not_the_healthcheck():
    c = prod_app().test_client()
    r = c.get("/login")
    assert r.status_code in (301, 302) and r.headers["Location"].startswith("https://")
    assert c.get("/healthz").status_code == 200                      # Railway probes over plain HTTP


def test_production_sets_hsts_and_a_locked_down_session_cookie_policy():
    app = prod_app()
    r = app.test_client().get("/login", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "panel.example.com"})
    assert r.status_code == 200 and "max-age" in r.headers["Strict-Transport-Security"]
    assert app.config["SESSION_COOKIE_SECURE"] is True and app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"            # Lax (not Strict): the cookie must survive the return from Google


def test_production_refuses_to_run_the_dev_stub_or_fake_aws():
    for bad in ({"AUTH_MODE": "dev"}, {"AWS_MODE": "fake"}):
        with pytest.raises(Exception, match="Refusing to start"):
            Settings.from_env({**PROD_ENV, **bad})


# ---------- breadcrumbs / navigation ----------

def test_dashboard_has_breadcrumb_trail_and_logo_link(authed):
    c, _ = authed
    html = c.get("/c/uniservice").get_data(as_text=True)
    nav = re.search(r'<nav class="crumbs" aria-label="Breadcrumb">(.*?)</nav>', html, re.S).group(1)
    assert re.search(r'<a href="/"[^>]*>.*?All Clients', nav, re.S)                    # first crumb goes home
    assert re.search(r'aria-current="page"[^>]*>\s*<span id="crumb-client">Uniservice</span>', nav)   # current page, not a link
    assert nav.count("<a ") == 1
    assert 'class="logo-link" href="/"' in html


def test_overview_has_logo_link_but_no_breadcrumb(authed):
    c, _ = authed
    html = c.get("/").get_data(as_text=True)
    assert 'class="logo-link" href="/"' in html and 'class="crumbs"' not in html


def test_breadcrumb_escapes_client_names():
    import json
    clients = {"clients": [{"slug": "x", "name": "<img src=x onerror=alert(1)>", "region": "ap-south-1",
                            "ec2_instances": [{"id": "i-0123456789abcdef0"}]}]}
    s = Settings.from_env({**ENV, "CLIENTS_JSON": json.dumps(clients)})
    app = create_app(s, FakeBackend(s.clients, speed=0))
    c = app.test_client(); sign_in(c)
    html = c.get("/c/x").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html


# ---------- theme ----------

def test_pages_default_to_dark_with_theme_script_loaded_before_css(app_backend, authed):
    c, _ = authed
    signed_out = app_backend[0].test_client()          # /login redirects when signed in
    for path in ("/login", "/", "/c/uniservice"):
        html = (signed_out if path == "/login" else c).get(path).get_data(as_text=True)
        assert 'data-theme="dark"' in html and 'data-bs-theme="dark"' in html, path
        script = re.search(r'<script src="/static/js/theme\.js"([^>]*)></script>', html)
        assert script and "defer" not in script.group(1) and "async" not in script.group(1), path   # sync => no flash
        assert script.start() < html.index('<link rel="stylesheet"'), path                         # before any CSS
        assert 'class="theme-toggle"' in html and 'aria-label="Switch to light theme"' in html, path
        assert "logo-on-light" in html and "logo-on-dark" in html, path


def test_theme_assets_are_served(client):
    assert client.get("/static/js/theme.js").status_code == 200
    r = client.get("/static/img/l180-logo-dark.svg")
    assert r.status_code == 200 and "svg" in r.content_type
    assert b"#F1F5FB" in r.data and b"#1B2A3D" not in r.data      # light text, not the navy that vanishes on dark


def test_stylesheet_has_both_themes(client):
    css = client.get("/static/css/style.css").get_data(as_text=True)
    assert 'color-scheme:dark' in css and ':root[data-theme="light"]' in css
    assert css.index("color-scheme:dark") < css.index(':root[data-theme="light"]')   # dark is the base, light overrides


# ---------- region placement ----------

def test_region_is_a_chip_next_to_the_client_name_not_in_the_top_bar(authed):
    c, _ = authed
    html = c.get("/c/uniservice").get_data(as_text=True)
    topbar = re.search(r'<header class="topbar.*?</header>', html, re.S).group(0)
    assert 'id="region"' not in topbar and 'class="region"' not in topbar    # the loose label is gone from the top bar
    title = re.search(r'<div class="intro-title">(.*?)</div>', html, re.S).group(1)
    assert '<h1 id="client-name" class="accent">Uniservice</h1>' in title                    # sits right beside the heading
    assert re.search(r'class="region-chip"[^>]*>.*?AWS region: </span><span id="region">ap-south-1</span>', title, re.S)


# ---------- left navigation ----------

def test_overview_marks_all_clients_current_and_lists_every_client(authed):
    c, _ = authed
    html = c.get("/").get_data(as_text=True)
    nav = re.search(r'<nav class="side-nav".*?</nav>', html, re.S).group(0)
    assert re.search(r'class="nav-item nav-all" href="/" aria-current="page"', nav)
    assert nav.count('class="nav-item nav-client"') == 2 and "aria-current" not in nav.split("nav-sub")[1]
    assert 'class="scrim"' in html and 'class="nav-burger"' in html and 'aria-controls="sidebar"' in html


def test_sidebar_escapes_client_names():
    import json
    clients = {"clients": [{"slug": "x", "name": "<img src=x onerror=alert(1)>", "region": "ap-south-1",
                            "ec2_instances": [{"id": "i-0123456789abcdef0"}]}]}
    s = Settings.from_env({**ENV, "CLIENTS_JSON": json.dumps(clients)})
    c = create_app(s, FakeBackend(s.clients, speed=0)).test_client()
    sign_in(c)
    html = c.get("/").get_data(as_text=True)
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_nav_script_is_served_and_loaded_after_common_js(authed):
    c, _ = authed
    assert c.get("/static/js/nav.js").status_code == 200
    html = c.get("/c/uniservice").get_data(as_text=True)
    assert html.index("js/common.js") < html.index("js/nav.js") < html.index("js/dashboard.js")


def test_overview_heading_says_all_clients_like_the_nav(authed):
    c, _ = authed
    html = c.get("/").get_data(as_text=True)
    assert re.search(r'<h1>All <span class="accent">Clients</span></h1>', html)
    assert 'class="kicker"' not in html                                   # no label line above the heading
    assert "<title>All Clients \u00b7 AWS Environment Control</title>" in html
