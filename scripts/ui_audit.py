"""UI audit: screenshots + automatic layout checks of every page/state at phone, tablet and desktop widths.

Runs the real app in-process on the fake backend with awkward data (long names, many resources, error/transition states).
    python scripts/ui_audit.py            -> shots/audit/*.png and a findings report (exit 1 if any finding)
"""
import itertools
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from aws_ops import FakeBackend  # noqa: E402
from config import Settings  # noqa: E402

OUT = ROOT / "shots" / "audit"
OUT.mkdir(parents=True, exist_ok=True)
LONG = "Extraordinarily Long Client Name Incorporated International Holdings"


def r(i, n):
    return {"id": i, "name": n}


CLIENTS = {"clients": [
    {"slug": "uniservice", "name": "Uniservice", "region": "ap-south-1",
     "rds_instances": [r("uniservice-db", "Primary database"), r("uniservice-rep", "Reporting database")],
     "ec2_instances": [r("i-0123456789abcdef0", "Web server"), r("i-0fedcba9876543210", "API server")]},
    {"slug": "harbor", "name": "Harbor Logistics", "region": "eu-west-1",
     "rds_instances": [r("harbor-db", "Main database")], "ec2_instances": [r("i-05d1c7a39e84b2f10", "Tracking app")]},
    {"slug": "longname", "name": LONG, "region": "ap-southeast-2",
     "rds_instances": [r("long-db-1", "A database with an unreasonably long display name that keeps going and going"),
                       r("long-db-2", "Reporting"), r("long-db-3", "Analytics warehouse")],
     "ec2_instances": [r("i-0a00000000000001a", "Web"), r("i-0a00000000000002b", "Background worker with a very long name indeed"),
                       r("i-0a00000000000003c", "Cron"), r("i-0a00000000000004d", "Queue consumer")]},
    {"slug": "solo", "name": "Northwind Retail", "region": "us-east-1", "ec2_instances": [r("i-06a8d3f5c1b97e204", "Storefront")]},
]}


class AuditBackend(FakeBackend):
    errors: dict = {}

    def describe(self, client, kind):
        rows = super().describe(client, kind)
        for row in rows:
            if row["id"] in self.errors:
                row["error"] = self.errors[row["id"]]
        return rows


def boot():
    s = Settings.from_env({"AWS_MODE": "fake", "CLIENTS_JSON": json.dumps(CLIENTS), "STATUS_CACHE_TTL": "0",
                           "RATE_LIMIT_ENABLED": "0", "SECRET_KEY": "audit", "FAKE_SPEED": "0.3",
                           "OP_POLL_INTERVAL": "0.5"})
    be = AuditBackend(s.clients, running={"uniservice", "longname", "solo"}, speed=0.3)
    app = create_app(s, be)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", be, srv, app


CHECK_JS = r"""() => {
  const vw = document.documentElement.clientWidth, out = [];
  const scrollX = document.documentElement.scrollWidth > vw + 1;
  if (scrollX) out.push(`page scrolls sideways (${document.documentElement.scrollWidth}px > ${vw}px)`);
  const desc = (e) => (e.id ? '#' + e.id : '') + '.' + [...e.classList].join('.') + ' "' + (e.textContent || '').trim().slice(0, 28) + '"';
  for (const e of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const b = e.getBoundingClientRect(); if (!b.width || !b.height) continue;
    if (e.closest('dialog:not([open]), template, .toasts')) continue;
    if (b.right > vw + 1 || b.left < -1) out.push('outside viewport: ' + desc(e) + ` [${Math.round(b.left)}..${Math.round(b.right)}]`);
    if (e.matches('button, a.btn-l, summary, .btn-icon, .crumbs a, .logo-link, .nav-item, .nav-toggle')) {
      if (e.offsetHeight < 38 && !e.closest('.toast')) out.push(`small tap target ${e.offsetWidth}x${e.offsetHeight}: ` + desc(e));
    }
    if (e.matches('.line, .bar, .glow, .visually-hidden')) continue;   // decorative / screen-reader-only: intentionally clipped
    if (e.children.length === 0 && e.scrollWidth > e.clientWidth + 1 && cs.textOverflow !== 'ellipsis' && cs.overflow !== 'visible')
      out.push('text clipped: ' + desc(e));
    if (e.children.length === 0 && cs.overflow === 'visible' && e.scrollWidth > e.clientWidth + 1 && e.clientWidth > 0)
      out.push('text spills out of its box: ' + desc(e));
  }
  // ---- nothing may overflow the sidebar's own box (catches content sizing itself to its text) ----
  const sb = document.querySelector('.sidebar');
  if (sb && getComputedStyle(sb).visibility !== 'hidden') {
    const sr = sb.getBoundingClientRect();
    for (const e of sb.querySelectorAll('*')) {
      const r = e.getBoundingClientRect(); if (!r.width || getComputedStyle(e).display === 'none') continue;
      if (r.right > sr.right + 1 || r.left < sr.left - 1) out.push('overflows the sidebar: ' + desc(e) + ` [${Math.round(r.left)}..${Math.round(r.right)} vs ${Math.round(sr.left)}..${Math.round(sr.right)}]`);
    }
  }

  // ---- text contrast (WCAG AA: 4.5:1, or 3:1 for large text), against the real composited background ----
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[,\s\/]+/).filter(Boolean).map(Number); return {r: p[0], g: p[1], b: p[2], a: p[3] === undefined ? 1 : p[3]}; };
  const over = (f, b) => ({r: f.r * f.a + b.r * (1 - f.a), g: f.g * f.a + b.g * (1 - f.a), b: f.b * f.a + b.b * (1 - f.a), a: 1});
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const ratio = (a, b) => { const l1 = lum(a), l2 = lum(b); return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05); };
  const bgOf = (el) => {
    const env = el.closest('.env');                                       // gradient banner: start from its mid navy
    const chain = []; for (let e = el; e && e !== env; e = e.parentElement) chain.push(e);
    let bg = env ? {r: 27, g: 42, b: 61, a: 1} : {r: 255, g: 255, b: 255, a: 1};
    for (let i = chain.length - 1; i >= 0; i--) { const c = parse(getComputedStyle(chain[i]).backgroundColor); if (c && c.a > 0) bg = over(c, bg); }
    return bg; };
  for (const e of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (!e.getBoundingClientRect().width) continue;
    if (e.closest('dialog:not([open]), template, .toasts, :disabled, [disabled]')) continue;
    if (!Array.from(e.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const bg = bgOf(e); let fg = parse(cs.color); if (!fg) continue; fg = over(fg, bg);
    const size = parseFloat(cs.fontSize), bold = parseInt(cs.fontWeight) >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5, got = ratio(fg, bg);
    if (got < need) out.push(`low contrast ${got.toFixed(2)}:1 (needs ${need}:1): ` + desc(e));
  }
  return out;
}"""

VIEWPORTS = {"phone": (390, 844), "tablet": (768, 1024), "desktop": (1280, 900)}
THEMES = ("dark", "light")


RUNNING = {"uniservice", "longname", "solo"}


def reset(be):
    be.errors = {}
    for c in CLIENTS["clients"]:
        for kind, key in (("rds", "rds_instances"), ("ec2", "ec2_instances")):
            for res in c.get(key, []):
                up = "available" if kind == "rds" else "running"
                be.set_state(c["slug"], kind, res["id"], up if c["slug"] in RUNNING else "stopped")


def run():
    """Run the whole audit; returns the sorted list of findings (empty = clean)."""
    base, be, srv, app = boot()
    svc = app.extensions["l180_service"]
    findings = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme, (vname, (w, h)) in itertools.product(THEMES, VIEWPORTS.items()):
            svc.join(60)
            svc._ops.clear()
            reset(be)
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if vname == "phone" else 1,
                                      is_mobile=vname == "phone")
            ctx.add_init_script(f"try{{localStorage.setItem('l180-theme','{theme}')}}catch(e){{}}")
            page = ctx.new_page()
            page.set_default_timeout(15000)
            label = f"{vname}-{theme}"

            def shot(name, full=True, label=label):
                page.wait_for_timeout(500)
                page.screenshot(path=str(OUT / f"{label}-{name}.png"), full_page=full)
                for f in page.evaluate(CHECK_JS):
                    findings.append(f"[{label}] {name}: {f}")

            page.goto(base + "/login")
            shot("login")
            page.goto(base + "/login/google")

            be.errors = {}
            page.goto(base + "/")
            page.wait_for_timeout(700)
            shot("overview")

            for slug in ("uniservice", "harbor", "solo"):
                page.goto(f"{base}/c/{slug}")
                page.wait_for_timeout(900)
                shot(f"dashboard-{slug}")

            # awkward data: long names, error row, transitions
            be.errors = {"long-db-2": "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."}
            be.set_state("longname", "rds", "long-db-3", "stopped")
            be.set_state("longname", "ec2", "i-0a00000000000003c", "stopping")
            be.set_state("longname", "ec2", "i-0a00000000000004d", "pending")
            page.goto(f"{base}/c/longname")
            page.wait_for_timeout(900)
            shot("dashboard-longname-mixed")
            page.goto(base + "/")
            page.wait_for_timeout(900)
            shot("overview-mixed")
            be.errors = {}
            for k, rid in (("rds", "long-db-3"), ("ec2", "i-0a00000000000003c"), ("ec2", "i-0a00000000000004d")):
                be.set_state("longname", k, rid, "available" if k == "rds" else "running")

            # open states
            page.goto(f"{base}/c/uniservice")
            page.wait_for_timeout(900)
            if w < 992:                                           # drawer: open it, check, close it
                page.get_by_role("button", name="Open menu").click()
                shot("nav-drawer-open", full=False)
                page.keyboard.press("Escape")
            else:                                                 # sidebar: also check the collapsed list
                page.locator(".nav-toggle").click()
                shot("nav-collapsed", full=False)
                page.locator(".nav-toggle").click()
                page.wait_for_timeout(200)
            page.locator("#btn-env-stop").click()
            page.locator("dialog#confirm").wait_for(state="visible")
            shot("confirm-dialog", full=False)
            page.get_by_role("button", name="Cancel").click()

            # toast + progress steps (real operation on the fake backend)
            page.locator("#btn-env-stop").click()
            page.locator("#confirm-ok").click()
            page.locator("#env-progress").wait_for(state="visible")
            page.wait_for_timeout(400)
            shot("dashboard-operation-running")
            ctx.close()
        browser.close()
    srv.shutdown()
    return sorted(set(findings))


def main():
    findings = run()
    print(f"screenshots: {OUT}")
    if findings:
        print(f"\n{len(findings)} finding(s):")
        for f in findings:
            print(" -", f)
        sys.exit(1)
    print("no layout findings")


if __name__ == "__main__":
    main()
