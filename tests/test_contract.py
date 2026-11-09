"""Contract test: the real backend must answer like aws-control-panel-design/preview/mock-api.js.

The mock is loaded into headless Chromium and driven through the same scenarios as the Flask app (same client
data). Responses must match: same status, same keys, same values. Allowed differences are listed explicitly:
  * mine may add keys (e.g. `error` in /api/overview rows)
  * operation id / step / message vary with timing
  * SHAPE_ONLY: steps where the real API is deliberately more accurate than the mock (reason given)
"""
import json
import re

import pytest

from app import create_app
from aws_ops import FakeBackend, ServiceError
from config import DESIGN_DIR, Settings

playwright = pytest.importorskip("playwright.sync_api")

MOCK_JS = (DESIGN_DIR / "preview" / "mock-api.js").read_text(encoding="utf-8")
DENIED = "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."


def res(i, n):
    return {"id": i, "name": n}


# identical data to the mock
CLIENTS = {"clients": [
    {"slug": "uniservice", "name": "Uniservice", "region": "ap-south-1",
     "rds_instances": [res("uniservice-db", "Primary database"), res("uniservice-reporting-db", "Reporting database")],
     "ec2_instances": [res("i-0a4f81c2e9b7d3056", "Web server"), res("i-0b7e22d4f1a96c835", "API server"),
                       res("i-03c91a5e7d2f48b60", "Background worker")]},
    {"slug": "harbor", "name": "Harbor Logistics", "region": "eu-west-1",
     "rds_instances": [res("harbor-db", "Main database")],
     "ec2_instances": [res("i-05d1c7a39e84b2f10", "Tracking app"), res("i-09e3b6c1a7f25d480", "Dispatch app")]},
    {"slug": "northwind", "name": "Northwind Retail", "region": "us-east-1",
     "ec2_instances": [res("i-06a8d3f5c1b97e204", "Storefront")]},
]}
RUNNING = {"uniservice", "northwind"}
P = "/api/c/uniservice"
WEB, API = "i-0a4f81c2e9b7d3056", "i-0b7e22d4f1a96c835"

FAIL = "FAIL_NEXT"
# name -> steps; a step is (method, path) or FAIL
SCENARIOS = {
    "overview": [("GET", "/api/overview")],
    "status-each-client": [("GET", "/api/c/uniservice/status"), ("GET", "/api/c/harbor/status"), ("GET", "/api/c/northwind/status")],
    "status-unknown-client": [("GET", "/api/c/nope/status")],
    "single-stop-then-conflict": [("POST", f"{P}/ec2/{WEB}/stop"), ("POST", f"{P}/ec2/{WEB}/stop"),
                                  ("POST", f"{P}/ec2/{WEB}/start"), ("GET", f"{P}/status")],
    "single-start-already-running": [("POST", f"{P}/ec2/{API}/start")],
    "single-db-already-available": [("POST", f"{P}/rds/uniservice-db/start")],
    "single-stop-already-stopped": [("POST", "/api/c/harbor/rds/harbor-db/stop")],
    "kind-stop-all-dbs": [("POST", f"{P}/rds/stop"), ("GET", f"{P}/status")],
    "kind-stop-twice": [("POST", f"{P}/ec2/stop"), ("POST", f"{P}/ec2/stop")],
    "kind-start-all-already": [("POST", f"{P}/ec2/start")],
    "kind-start-harbor-apps": [("POST", "/api/c/harbor/ec2/start")],
    "unknown-resource": [("POST", f"{P}/ec2/i-0000000000000000f/stop")],
    "unknown-client-write": [("POST", "/api/c/nope/environment/start")],
    "env-start-with-db": [("POST", "/api/c/harbor/environment/start"), ("GET", "/api/c/harbor/status"), ("GET", "/api/overview")],
    "env-stop-with-db": [("POST", f"{P}/environment/stop"), ("GET", f"{P}/status")],
    "env-stop-no-db": [("POST", "/api/c/northwind/environment/stop"), ("GET", "/api/c/northwind/status")],
    "env-conflict-while-running": [("POST", "/api/c/harbor/environment/start"), ("POST", "/api/c/harbor/environment/stop"),
                                   ("POST", "/api/c/harbor/ec2/start")],
    "aws-denied": [FAIL, ("POST", f"{P}/ec2/{WEB}/stop")],
}

# (scenario, step index) where the real API is deliberately more accurate than the mock: status and keys must
# still match, wording may differ.
SHAPE_ONLY = {
    ("kind-stop-twice", 1): "mock says 'Every application is already stopped' while they are still stopping; "
                            "real says 'Some applications are still changing state'",
}
IGNORED_PATHS = re.compile(r"operation\.(id|step|message)$")


def conform(mock, mine, path="$"):
    """Everything in `mock` must be present and equal in `mine` (mine may have extra keys)."""
    if IGNORED_PATHS.search(path.replace("$.", "")):
        return
    if isinstance(mock, dict):
        assert isinstance(mine, dict), f"{path}: expected object, got {mine!r}"
        for k, v in mock.items():
            assert k in mine, f"{path}.{k}: missing in real API response {json.dumps(mine)[:300]}"
            conform(v, mine[k], f"{path}.{k}")
    elif isinstance(mock, list):
        assert isinstance(mine, list) and len(mine) == len(mock), f"{path}: list mismatch {mock!r} vs {mine!r}"
        for i, (a, b) in enumerate(zip(mock, mine)):
            conform(a, b, f"{path}[{i}]")
    else:
        assert mock == mine, f"{path}: mock={mock!r} real={mine!r}"


@pytest.fixture(scope="module")
def browser_page():
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # browser not installed
            pytest.skip(f"Chromium not available: {e}")
        yield browser.new_page()
        browser.close()


def run_mock(page, steps):
    page.goto("about:blank")
    page.add_script_tag(content=MOCK_JS)
    out = []
    for s in steps:
        if s == FAIL:
            page.evaluate("window.__mock.failNext = true")
            continue
        method, path = s
        out.append(page.evaluate(
            """async ([m, p]) => { const r = await fetch(p, {method: m}); return {status: r.status, body: await r.json()}; }""",
            [method, path]))
    return out


def run_real(steps):
    env = {"AWS_MODE": "fake", "STATUS_CACHE_TTL": "0", "RATE_LIMIT_ENABLED": "0", "SECRET_KEY": "k",
           "CLIENTS_JSON": json.dumps(CLIENTS), "FAKE_SPEED": "1", "OP_POLL_INTERVAL": "5"}
    settings = Settings.from_env(env)
    backend = FakeBackend(settings.clients, running=RUNNING)
    app = create_app(settings, backend)
    c = app.test_client()
    c.get("/login/google")
    html = c.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    out = []
    for s in steps:
        if s == FAIL:
            backend.fail_next = ServiceError(DENIED, 403)
            continue
        method, path = s
        r = c.get(path) if method == "GET" else c.post(path, headers={"X-CSRF-Token": token})
        out.append({"status": r.status_code, "body": r.get_json()})
    return out


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_matches_mock(browser_page, name):
    steps = SCENARIOS[name]
    mock, real = run_mock(browser_page, steps), run_real(steps)
    assert len(mock) == len(real)
    for i, (m, r) in enumerate(zip(mock, real)):
        label = f"{name}[{i}] {[s for s in steps if s != FAIL][i]}"
        assert m["status"] == r["status"], f"{label}: status mock={m['status']} real={r['status']}\n mock={m['body']}\n real={r['body']}"
        if (name, i) in SHAPE_ONLY:
            assert set(m["body"]) <= set(r["body"]), label
            continue
        try:
            conform(m["body"], r["body"])
        except AssertionError as e:
            raise AssertionError(f"{label}: {e}") from None
