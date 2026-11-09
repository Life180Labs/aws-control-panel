"""End-to-end: the real frontend in headless Chromium against the real Flask app (AWS_MODE=fake, sped up).

Fails on any console error, CSP violation, failed request or unexpected HTTP error. Screenshots go to shots/.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
expect.set_options(timeout=30000)
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "shots"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(extra_env=None):
    """Start the app (fake AWS, sped up) on a free port; returns (process, base_url) once /healthz answers."""
    port = free_port()
    env = {**os.environ, "AUTH_MODE": "dev", "AWS_MODE": "fake", "FAKE_SPEED": "0.25", "OP_POLL_INTERVAL": "0.5",
           "STATUS_CACHE_TTL": "1", "CLIENTS_FILE": str(ROOT / "clients.example.json"), "PORT": str(port), "SECRET_KEY": "e2e",
           "RATE_LIMIT_ENABLED": "0", "PYTHONUNBUFFERED": "1", **(extra_env or {})}
    for k in ("RAILWAY_ENVIRONMENT", "PRODUCTION"):
        env.pop(k, None)
    if not (extra_env or {}).get("CLIENTS_JSON"):
        env.pop("CLIENTS_JSON", None)
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("server exited:\n" + proc.stdout.read())
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def server():
    proc, base = launch()
    yield base
    stop(proc)


ONE_CLIENT = ('{"clients":[{"slug":"solo","name":"Solo Co","region":"ap-south-1",'
              '"ec2_instances":[{"id":"i-0123456789abcdef0","name":"App"}]}]}')


@pytest.fixture(scope="module")
def one_client_server():
    proc, base = launch({"CLIENTS_JSON": ONE_CLIENT})
    yield base
    stop(proc)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


class Watch:
    """Collects everything that would be a bug a user could hit."""
    def __init__(self, page):
        self.problems = []
        page.on("console", lambda m: self.problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        # ERR_ABORTED = the browser cancelled an in-flight load because the test navigated away: not a failure.
        # Real failures (connection errors, HTTP 4xx/5xx below) are still reported.
        page.on("requestfailed", lambda r: self.problems.append(f"requestfailed: {r.url} {r.failure}")
                if "ERR_ABORTED" not in str(r.failure) else None)
        page.on("response", lambda r: self.problems.append(f"http {r.status}: {r.url}")
                if r.status >= 400 else None)


def test_full_user_journey(server, browser):
    SHOTS.mkdir(exist_ok=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(30000)
    watch = Watch(page)

    # ---- sign in ----
    page.goto(server + "/")
    assert page.url.endswith("/login")
    assert page.get_by_role("heading", name="AWS Environment Control").is_visible()
    page.screenshot(path=str(SHOTS / "1-login.png"))
    page.get_by_role("link", name="Sign in with Google").click()
    page.wait_for_url(server + "/")

    # ---- overview ----
    expect(page.locator("#tally")).not_to_have_text("Checking environments")
    assert "1 of 2 environments running" in page.locator("#tally").inner_text()
    cards = page.locator(".client")
    assert cards.count() == 2
    uni, other = cards.nth(0), cards.nth(1)
    assert uni.locator(".pill").inner_text().strip() == "Running"
    assert other.locator(".pill").inner_text().strip() == "Stopped"
    assert "All 2 available" in uni.inner_text() and "All 2 running" in uni.inner_text()
    assert other.locator("[data-res=rds]").is_hidden()                     # client without databases
    assert other.locator(".js-start").is_enabled() and other.locator(".js-stop").is_disabled()
    assert uni.locator(".js-start").is_disabled() and uni.locator(".js-stop").is_enabled()
    page.screenshot(path=str(SHOTS / "2-overview.png"), full_page=True)

    # fonts actually loaded (static + MIME + CSP all working)
    assert page.evaluate("document.fonts.load('700 16px \"Plus Jakarta Sans\"').then(f => f.length > 0)")

    # ---- start another client from the overview ----
    other.locator(".js-start").click()
    page.locator(".toast[data-kind=success]").first.wait_for()
    expect(page.locator("#clients .client:nth-child(2) .pill").first).to_have_text("Running")
    assert "2 of 2 environments running" in page.locator("#tally").inner_text()

    # ---- stop with confirmation: cancel first ----
    other.locator(".js-stop").click()
    dlg = page.locator("dialog#confirm")
    dlg.wait_for(state="visible")
    assert "Stop everything for Another Client?" in dlg.locator("h2").inner_text()
    page.screenshot(path=str(SHOTS / "3-confirm.png"))
    dlg.get_by_role("button", name="Cancel").click()
    dlg.wait_for(state="hidden")
    assert other.locator(".pill").inner_text().strip() == "Running"        # nothing happened

    # ---- manage Uniservice ----
    uni.get_by_role("link", name="Manage").click()
    page.wait_for_url(server + "/c/uniservice")
    expect(page.locator("#env-state").first).to_have_text("Running")
    assert page.locator("#group-rds .row").count() == 2 and page.locator("#group-ec2 .row").count() == 2
    assert page.locator("#group-rds .row-name").first.inner_text() == "Primary database"
    assert page.locator("#region").inner_text() == "ap-south-1"
    assert page.locator("#btn-env-start").is_disabled() and page.locator("#btn-env-stop").is_enabled()
    page.screenshot(path=str(SHOTS / "4-dashboard-running.png"), full_page=True)

    # single database stop (confirm), then start again
    first_db = page.locator("#group-rds .row").first
    first_db.locator(".js-stop").click()
    assert "Stop Primary database for Uniservice?" in page.locator("#confirm-title").inner_text()
    page.locator("#confirm-ok").click()
    page.locator(".toast[data-kind=success]", has_text="Stopping Primary database").wait_for()
    expect(page.locator("#group-rds .row .pill").first).to_have_text("Stopped")
    assert page.locator("#env-state").inner_text().strip() == "Partly running"
    first_db.locator(".js-start").click()
    expect(page.locator("#group-rds .row .pill").first).to_have_text("Available")

    # ---- stop everything: progress steps, order, completion ----
    page.locator("#btn-env-stop").click()
    page.locator("#confirm-ok").click()
    page.locator("#env-progress").wait_for(state="visible")
    labels = page.locator("#env-steps .step").all_inner_texts()
    assert [l.strip() for l in labels] == ["Stop applications", "Wait until stopped", "Stop databases"]
    page.screenshot(path=str(SHOTS / "5-dashboard-stopping.png"), full_page=True)
    expect(page.locator("#env-state").first).to_have_text("Stopped", timeout=60000)
    page.locator(".toast", has_text="The environment is stopped.").wait_for()
    assert page.locator("#btn-env-stop").is_disabled() and page.locator("#btn-env-start").is_enabled()
    page.screenshot(path=str(SHOTS / "6-dashboard-stopped.png"), full_page=True)

    # ---- start everything: databases first ----
    page.locator("#btn-env-start").click()
    page.locator("#env-progress").wait_for(state="visible")
    assert [l.strip() for l in page.locator("#env-steps .step").all_inner_texts()] == \
        ["Start databases", "Wait until available", "Start applications"]
    expect(page.locator("#env-state").first).to_have_text("Running", timeout=60000)
    page.locator(".toast", has_text="The environment is running.").wait_for()

    # ---- left navigation: switch client ----
    page.locator(".nav-client[data-slug=another-client]").click()
    page.wait_for_url(server + "/c/another-client")
    expect(page.locator("#env-state").first).to_have_text("Running")
    assert page.locator("#group-rds").is_hidden() and page.locator("#group-ec2 .row").count() == 1
    assert page.locator("#group-ec2 .group-actions").is_hidden()           # "Start all/Stop all" only with 2+

    # ---- refresh button, then sign out ----
    page.locator("#btn-refresh").click()
    expect(page.locator("#updated")).to_contain_text("Updated")
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(server + "/login")
    page.goto(server + "/c/uniservice")
    assert page.url.endswith("/login")                                       # session really ended

    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_mobile_layout_has_no_horizontal_scroll(server, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
    page = ctx.new_page()
    page.set_default_timeout(30000)
    watch = Watch(page)
    page.goto(server + "/login/google")
    expect(page.locator("#tally")).not_to_have_text("Checking environments")
    for name, url in (("overview", "/"), ("dashboard", "/c/uniservice")):
        page.goto(server + url)
        page.wait_for_timeout(800)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"{name} scrolls sideways"
        page.screenshot(path=str(SHOTS / f"m-{name}.png"), full_page=True)
    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_session_expiry_sends_browser_to_login(server, browser):
    """A 401 from the API must bounce the user to /login (handled by common.js)."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(server + "/login/google")
    page.wait_for_url(server + "/")
    ctx.clear_cookies()
    page.locator("#ov-refresh").click()
    page.wait_for_url(server + "/login")
    ctx.close()


def test_breadcrumb_and_logo_navigation(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    watch = Watch(page)
    page.goto(server + "/login/google")
    page.goto(server + "/c/another-client")
    crumbs = page.get_by_role("navigation", name="Breadcrumb")
    expect(crumbs).to_contain_text("All Clients")
    expect(crumbs.locator("[aria-current=page]")).to_have_text("Another Client")
    page.screenshot(path=str(SHOTS / "7-breadcrumb.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 330})
    crumbs.get_by_role("link", name="All Clients").click()
    page.wait_for_url(server + "/")
    expect(page.locator("#tally")).not_to_have_text("Checking environments")
    page.goto(server + "/c/uniservice")
    page.get_by_role("link", name="L180 Labs: All Clients").click()      # logo is a home link too
    page.wait_for_url(server + "/")
    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_theme_defaults_to_dark_toggles_and_persists(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})   # fresh context = empty localStorage
    page = ctx.new_page()
    watch = Watch(page)
    theme = lambda: page.evaluate("document.documentElement.dataset.theme")                       # noqa: E731
    canvas = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")             # noqa: E731

    page.goto(server + "/login")
    assert theme() == "dark" and canvas() == "rgb(11, 18, 32)"
    toggle = page.get_by_role("button", name="Switch to light theme")
    expect(toggle).to_be_visible()
    expect(page.locator(".logo-on-dark")).to_be_visible()
    expect(page.locator(".logo-on-light")).to_be_hidden()
    page.screenshot(path=str(SHOTS / "8-theme-dark-login.png"))

    toggle.click()                                                   # -> light
    assert theme() == "light" and canvas() == "rgb(252, 252, 250)"
    expect(page.get_by_role("button", name="Switch to dark theme")).to_be_visible()
    expect(page.locator(".logo-on-light")).to_be_visible()
    expect(page.locator(".logo-on-dark")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('l180-theme')") == "light"
    page.screenshot(path=str(SHOTS / "9-theme-light-login.png"))

    page.reload()                                                    # survives a reload...
    assert theme() == "light"
    page.get_by_role("link", name="Sign in with Google").click()      # ...and navigation to other pages
    page.wait_for_url(server + "/")
    assert theme() == "light"
    page.goto(server + "/c/uniservice")
    assert theme() == "light"
    expect(page.locator("#env-state")).to_have_text("Running")

    page.get_by_role("button", name="Switch to dark theme").click()   # -> dark again, on the dashboard
    assert theme() == "dark"
    page.reload()
    assert theme() == "dark" and page.evaluate("localStorage.getItem('l180-theme')") == "dark"
    page.screenshot(path=str(SHOTS / "10-theme-dark-dashboard.png"))

    # a second tab picks up the change through the storage event
    other = ctx.new_page()
    other.goto(server + "/")
    assert other.evaluate("document.documentElement.dataset.theme") == "dark"
    page.bring_to_front()
    page.get_by_role("button", name="Switch to light theme").click()
    other.wait_for_timeout(300)
    assert other.evaluate("document.documentElement.dataset.theme") == "light"

    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_invalid_stored_theme_falls_back_to_dark(server, browser):
    ctx = browser.new_context()
    ctx.add_init_script("try{localStorage.setItem('l180-theme','neon')}catch(e){}")
    page = ctx.new_page()
    page.goto(server + "/login")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    ctx.close()


def test_sidebar_navigation_on_desktop(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    watch = Watch(page)
    page.goto(server + "/login/google")
    page.goto(server + "/")

    side = page.locator("#sidebar")
    expect(side).to_be_visible()
    expect(page.locator(".nav-burger")).to_be_hidden()                       # no hamburger on desktop
    all_clients = side.locator(".nav-all")
    expect(all_clients).to_have_attribute("aria-current", "page")
    expect(side.get_by_text("admin@example.com")).to_be_visible()
    expect(side.get_by_role("button", name="Sign out")).to_be_visible()

    # expanded by default: both clients listed, with live status dots
    toggle = side.get_by_role("button", name="Collapse client list")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    uni, other = page.locator(".nav-client[data-slug=uniservice]"), page.locator(".nav-client[data-slug=another-client]")
    expect(uni).to_be_visible()
    expect(other).to_be_visible()
    # dots mirror /api/overview (the shared test server may be in any state, so read it instead of assuming)
    overview = page.evaluate("fetch('/api/overview').then(r => r.json())")
    expected = {}
    for c in overview["clients"]:
        states = {r["state"] for r in c["rds"] + c["ec2"]}
        expected[c["slug"]] = "ok" if states <= {"running", "available"} else "stopped" if states == {"stopped"} else None
    assert expected["uniservice"] and expected["another-client"], expected          # both settled (running or stopped)
    expect(uni.locator(".nav-dot")).to_have_attribute("data-state", expected["uniservice"])
    expect(other.locator(".nav-dot")).to_have_attribute("data-state", expected["another-client"])
    expect(uni).to_contain_text("Uniservice")
    title = {"ok": "Running", "stopped": "Stopped"}
    assert title[expected["uniservice"]] in uni.get_attribute("title")
    assert title[expected["another-client"]] in other.get_attribute("title")
    page.screenshot(path=str(SHOTS / "11-sidebar-expanded.png"))

    # collapse -> hidden, remembered across reload and pages; expand again
    toggle.click()
    expect(uni).to_be_hidden()
    expect(side.get_by_role("button", name="Expand client list")).to_have_attribute("aria-expanded", "false")
    page.screenshot(path=str(SHOTS / "12-sidebar-collapsed.png"))
    page.reload()
    expect(uni).to_be_hidden()
    page.goto(server + "/c/uniservice")
    expect(page.locator(".nav-client[data-slug=uniservice]")).to_be_hidden()
    side.get_by_role("button", name="Expand client list").click()
    expect(page.locator(".nav-client[data-slug=uniservice]")).to_be_visible()

    # current client is highlighted and navigation works both ways
    expect(page.locator(".nav-client[data-slug=uniservice]")).to_have_attribute("aria-current", "page")
    expect(all_clients).not_to_have_attribute("aria-current", "page")
    page.locator(".nav-client[data-slug=another-client]").click()
    page.wait_for_url(server + "/c/another-client")
    expect(page.locator(".nav-client[data-slug=another-client]")).to_have_attribute("aria-current", "page")
    page.locator(".nav-all").click()
    page.wait_for_url(server + "/")

    # the dot follows reality: flip the other client from the overview; its sidebar dot changes on a later poll
    dot = page.locator(".nav-client[data-slug=another-client] .nav-dot")
    card = page.locator(".client[data-slug=another-client]")
    if expected["another-client"] == "ok":
        card.locator(".js-stop").click()
        page.locator("#confirm-ok").click()
        expect(dot).to_have_attribute("data-state", "stopped", timeout=45000)
    else:
        card.locator(".js-start").click()
        expect(dot).to_have_attribute("data-state", "ok", timeout=45000)

    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_sidebar_drawer_on_phone(server, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    page = ctx.new_page()
    watch = Watch(page)
    page.goto(server + "/login/google")
    page.goto(server + "/c/uniservice")
    burger, side = page.get_by_role("button", name="Open menu"), page.locator("#sidebar")

    expect(burger).to_be_visible()
    expect(side).to_be_hidden()                                              # closed drawer is not reachable
    expect(page.locator("#env-state")).to_have_text("Running")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    burger.click()                                                           # open
    expect(side).to_be_visible()
    expect(burger).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("document.querySelector('.app-main > main').inert === true")          # page behind is inert
    assert page.evaluate("document.activeElement.classList.contains('nav-close')")             # focus moved into the drawer
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(side).to_have_css("transform", re.compile(r"^(none|matrix\(1, 0, 0, 1, 0, 0\))$"))   # slide-in finished
    box = side.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390
    page.screenshot(path=str(SHOTS / "13-drawer-open-phone.png"))

    page.keyboard.press("Escape")                                            # Escape closes and returns focus
    expect(side).to_be_hidden()
    assert page.evaluate("document.querySelector('.app-main > main').inert === false")
    assert page.evaluate("document.activeElement.classList.contains('nav-burger')")

    burger.click()                                                           # tapping the dimmed area closes it
    expect(side).to_be_visible()
    page.mouse.click(375, 400)
    expect(side).to_be_hidden()

    burger.click()                                                           # the close button works
    page.get_by_role("button", name="Close menu").click()
    expect(side).to_be_hidden()

    burger.click()                                                           # theme toggle + user live in the drawer
    page.get_by_role("button", name="Switch to light theme").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.get_by_role("button", name="Switch to dark theme").click()
    expect(side.get_by_text("admin@example.com")).to_be_visible()

    side.locator(".nav-all").click()                                          # a link closes the drawer and navigates
    page.wait_for_url(server + "/")
    expect(page.locator("#sidebar")).to_be_hidden()

    burger.click()
    side.get_by_role("button", name="Sign out").click()                      # sign out from the drawer
    page.wait_for_url(server + "/login")

    assert watch.problems == [], "\n".join(watch.problems)
    ctx.close()


def test_drawer_closes_when_resized_to_desktop(server, browser):
    ctx = browser.new_context(viewport={"width": 700, "height": 800})
    page = ctx.new_page()
    page.goto(server + "/login/google")
    page.get_by_role("button", name="Open menu").click()
    expect(page.locator("#sidebar")).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 800})
    expect(page.locator("body")).not_to_have_class(re.compile(r"nav-open"))        # the resize event arrives asynchronously
    expect(page.locator(".app-main > main")).to_have_js_property("inert", False)
    expect(page.locator("#sidebar")).to_be_visible()                         # visible as the permanent sidebar
    ctx.close()


def test_overview_heading_matches_the_nav_label(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(server + "/login/google")
    page.goto(server + "/")
    nav_label = page.locator(".nav-all .nav-label").inner_text()
    heading = page.get_by_role("heading", level=1)
    expect(heading).to_have_text("All Clients")
    assert heading.inner_text() == nav_label == "All Clients"                  # page title == nav item == breadcrumb root
    expect(page.locator(".intro .kicker")).to_have_count(0)                      # heading is the first line
    expect(page).to_have_title("All Clients \u00b7 AWS Environment Control")
    page.goto(server + "/c/uniservice")
    expect(page.locator(".crumbs li").first).to_have_text("All Clients")        # same name in the breadcrumb
    ctx.close()


def test_one_client_wording_is_singular_and_counts_agree(one_client_server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(one_client_server + "/login/google")
    page.goto(one_client_server + "/")
    expect(page.locator(".nav-count")).to_have_text("1")
    expect(page.locator(".nav-client")).to_have_count(1)
    expect(page.locator("#tally")).to_have_text("1 of 1 environment running")  # not "environments"
    ctx.close()


@pytest.mark.parametrize("width", [992, 1280, 1920])
def test_page_heading_is_level_with_the_all_clients_menu_item(server, browser, width):
    """The first line of each page sits on the same horizontal line as the sidebar's "All Clients" item."""
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    centre = lambda sel: page.evaluate(                                                             # noqa: E731
        "s => { const r = document.querySelector(s).getBoundingClientRect(); return r.top + r.height / 2 }", sel)
    page.goto(server + "/login/google")
    page.goto(server + "/")
    assert abs(centre(".intro h1") - centre(".nav-all")) <= 1.5, "overview heading is not level with the menu item"
    page.screenshot(path=str(SHOTS / f"14-aligned-overview-{width}.png"), clip={"x": 0, "y": 0, "width": min(width, 1280), "height": 260})
    page.goto(server + "/c/uniservice")
    assert abs(centre(".crumbs a") - centre(".nav-all")) <= 1.5, "breadcrumb row is not level with the menu item"
    ctx.close()


def test_region_chip_is_right_aligned_on_wide_screens_and_left_on_phones(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    edges = lambda sel: page.evaluate(                                                               # noqa: E731
        "s => { const r = document.querySelector(s).getBoundingClientRect(); return [r.left, r.right, r.top + r.height / 2] }", sel)
    page.goto(server + "/login/google")
    page.goto(server + "/c/uniservice")
    expect(page.locator(".region-chip")).to_be_visible()
    _, chip_right, chip_mid = edges(".region-chip")
    _, banner_right, _ = edges(".env")
    _, _, h1_mid = edges(".intro h1")
    assert abs(chip_right - banner_right) <= 1.5, "chip is not on the right edge of the content column"
    assert abs(chip_mid - h1_mid) <= 2, "chip is not vertically centred on the heading"
    page.screenshot(path=str(SHOTS / "15-region-right.png"), clip={"x": 0, "y": 0, "width": 1280, "height": 300})

    page.set_viewport_size({"width": 390, "height": 844})             # phone: stays with the name, not floating right
    chip_left, chip_right, _ = edges(".region-chip")
    h1_left, _, _ = edges(".intro h1")
    assert chip_left < 390 / 2 or abs(chip_left - h1_left) <= 1.5, "chip should stay left on phones"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    ctx.close()


def test_client_name_heading_uses_the_accent_orange_in_both_themes(server, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    colour = lambda sel: page.evaluate("s => getComputedStyle(document.querySelector(s)).color", sel)   # noqa: E731
    page.goto(server + "/login/google")
    page.goto(server + "/c/uniservice")
    assert colour("#client-name") == "rgb(247, 146, 10)"            # dark theme: brand amber, same as "Clients" on the overview
    page.get_by_role("button", name="Switch to light theme").click()
    assert colour("#client-name") == "rgb(217, 119, 6)"             # light theme: deeper amber that stays legible on white
    page.get_by_role("button", name="Switch to dark theme").click()
    assert colour("#client-name") == colour(".accent")
    page.goto(server + "/")
    assert colour(".intro h1 .accent") == "rgb(247, 146, 10)"       # consistent with the overview's accent word
    ctx.close()
