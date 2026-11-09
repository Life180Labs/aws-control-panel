# PROGRESS: AWS Environment Control backend (Phase 1)

Working log for the Flask backend. The frontend lives in `aws-control-panel-design/` and is the source of truth;
Flask serves its `templates/` and `static/` directly. Exactly **one** frontend edit was needed (see "Frontend changes").

## STATUS: Phase 1 complete and verified. Waiting at the gate for "APPROVED FOR SSO".

Final evidence (3 consecutive full runs):
```
$ .venv/Scripts/python -m pytest tests -q
116 passed in 53.29s        (run 1: 116 passed in 56.87s, run 3: 116 passed in 53.26s)
$ .venv/Scripts/python -m pyflakes app.py api.py auth.py config.py aws_ops.py operations.py tests/*.py
(no output: lint clean)
```

## UI polish + responsive audit (2026-11-09, requested by the owner)
Trigger: the arrow between the Databases and Applications cards rendered badly. Built `scripts/ui_audit.py`
(runs the app in-process on the fake backend with awkward data: very long names, 3-4 resources per group, error / starting /
stopping states) which screenshots login, overview, dashboards, switcher-open, confirm dialog and a running operation at
phone (390), tablet (768) and desktop (1280), and checks for sideways scroll, elements outside the viewport, clipped or
spilling text and tap targets under 38px. It is also a pytest (`tests/test_ui_layout.py`, ~1 min). Screenshots: `shots/audit/`.

Defects found and fixed (all in `aws-control-panel-design/static/css/style.css` unless noted):
| Defect | Fix |
|---|---|
| Connector arrow: 2px line, 9px chevron jammed against the next card, stray glow dot while idle | 3px line fading to cyan, 14px chevron with 8px clearance, dot hidden when idle |
| Long client name widened the top bar: the whole page scrolled sideways (734px on a 390px phone, 953px on a 768px tablet) | switcher label shrinks with an ellipsis; region/user/sign-out never wrap |
| Switcher menu could run off-screen with long names | width-capped; anchored to the page edge on tablet, full width under the top bar on phone; long names ellipsize |
| Bootstrap `.row` negative margins + child padding broke resource rows (dividers poked out of the card, status pills hugged the edge) | `.row{--bs-gutter-x:0;--bs-gutter-y:0}` |
| Refresh button was a 34px tap target | 40px |
| Icon-only "Sign out" on phones had no accessible name | `aria-label="Sign out"` in `dashboard.html` and `overview.html` |
| Very long unbroken names could overflow headings | `overflow-wrap:anywhere` |
| Phone confirm-dialog buttons | equal width |
Earlier the same session: `.toasts .toast{display:flex}` (Bootstrap hid every toast).

Breadcrumbs (requested after the audit): client dashboard now shows `[grid icon] All clients > <client>` above the heading
(replaces the plain "AWS Environment Control" label; the product name stays in the page title and sign-in page). The first
crumb links to the overview; the current page is not a link (`aria-current="page"`) and truncates with an ellipsis. The logo is now
a link to All clients on both pages. Overview has no breadcrumb (it is the root; one crumb would only repeat the heading).
Templates: `dashboard.html`, `overview.html`, `_icons.html` (new `i-grid` icon); CSS appended to `style.css`.
Found by eye, not by the numeric checks: with a very long client name on a phone, "All clients" wrapped onto two lines; fixed
(first crumb never wraps). Crumb link tap height is 40px at every width. New tests: crumb markup + HTML-escaping of client names
(`test_app.py`), click-through of crumb and logo in a real browser (`test_e2e_browser.py`). Suite: 121 passed.

Result: audit reports no findings at any width; screenshots reviewed by eye (arrow close-up, phone dashboard/overview/menu/
dialog/login, tablet dashboard). Full suite: 117 passed.

Not changed: `aws-control-panel-design/preview/index.html` embeds its own inlined copy of the old CSS, so the standalone
mock preview does not have these fixes. The Flask app (the real product) does.
Also fixed: tests were reading the developer's local `clients.json` (real resource IDs) instead of `clients.example.json`; they
are now pinned to the example file. (The tests only ever used the fake backend; nothing touched real AWS.)

## Dark theme by default + light toggle (2026-11-09, requested by the owner)
- **Dark is the default** (also with JS off / no saved choice). A sun/moon button in the top bar (and top-right on the sign-in page)
  switches to light; the choice is saved in `localStorage` (`l180-theme`), applies to every page, and syncs across open tabs.
  It deliberately does not follow the OS setting: dark unless the user picks light.
- **No flash**: `static/js/theme.js` loads synchronously in `<head>` before the stylesheet (CSP-safe, no inline script) and sets
  `data-theme` / `data-bs-theme` and `<meta name="theme-color">`.
- **CSS**: every colour that differs between themes is now a token. `:root` holds the dark values, `:root[data-theme="light"]` holds
  the original light palette. Brand constants (navy banner, amber, cyan, white-on-red danger button, toasts text) are shared.
  Status pills, badges, notices, alerts, shadows, backdrops, the confirm dialog, switcher menu, breadcrumbs all have dark variants.
- **Logo**: new `l180-logo-dark.svg` (light text; the navy text vanished on dark). Both are in the page, CSS shows the right one.
- **Audit extended**: `scripts/ui_audit.py` / `tests/test_ui_layout.py` now run both themes at three widths (6 passes) and add an automatic
  WCAG contrast check (4.5:1, or 3:1 for large text) against the real composited background.
  Contrast fixes made along the way: banner step labels not yet reached `#7F90A8 -> #94A4BB` (4.47:1 in both themes);
  light theme only: user avatar letter `#2F6FED -> #1F5FDB` (3.9:1) and the amber "Control" in titles `#F7920A -> #D97706` on white (2.3:1);
  brand amber is unchanged in dark.
- New tests (126 total, all passing): dark default + sync script before CSS + toggle markup (`test_app.py`); default/toggle/persistence
  across reload and pages/cross-tab sync/invalid stored value (`test_e2e_browser.py`).
- Not changed: `preview/index.html` (standalone mock) still has its own inlined old light CSS.

## Region moved out of the top bar (2026-11-09, requested by the owner)
The AWS region was a loose label in the top bar, detached from anything. It is now a small chip (globe icon + region) beside the client
name in the dashboard heading, matching the overview cards where the region sits under the client name. It is rendered server-side
(`dashboard.html`, new `i-globe` icon, `.intro-title` / `.region-chip` CSS) and `dashboard.js` still refreshes `#region` from the status
response. Side effect: the region is now visible on phones (the old label was hidden below 576px); with a very long client name the chip
wraps under the heading. Removed the dead `.region` CSS. Audit: `.visually-hidden` (the screen-reader "AWS region:" prefix) is skipped as
intentionally clipped. New test in `test_app.py`. Suite: 127 passed.

## Left navigation pane (2026-11-09, requested by the owner)
- **Desktop (>=992px)**: fixed left sidebar (`templates/_sidebar.html`): logo; **All clients** (links to the overview, count badge, chevron to
  expand/collapse) with the individual clients underneath, each with a live status dot; footer with the user (name + email), theme toggle and
  Sign out. The current page is highlighted (`aria-current="page"`). Expanded/collapsed state is remembered (`l180-nav-open`).
- **Tablet/phone (<992px)**: the sidebar is a slide-in drawer opened from a slim sticky top bar (menu button + logo). Dimmed backdrop; closes on Esc,
  backdrop tap, the close button, a link click, or resizing to desktop. While open, the page behind is `inert`, focus moves into the drawer and
  returns to the menu button on close. Closed drawer is `visibility:hidden` (not tabbable).
- **Live status dots**: `static/js/nav.js` polls `/api/overview` (15s, 4s while something is starting/stopping, paused in background tabs) and
  colours each client's dot with the same state logic as the rest of the UI (`L180.summarize`); each link gets a screen-reader status suffix.
- **Replaced**: the top-right client dropdown and the old top-bar controls (user, theme, sign out moved to the sidebar footer). Dead CSS removed.
  Breadcrumbs and the region chip stay.
- **Bug found by the audit/screenshots**: Bootstrap defines `.nav` as `display:flex`, which made my nav container size to its content (539px in a 320px
  drawer): long names did not truncate and the chevron/count were pushed out of view. Renamed to `.side-nav`. (Third Bootstrap class collision
  after `.toast` and `.row`: `.nav`, `.row`, `.toast` are all Bootstrap names.)
- **Audit extended**: new check that nothing overflows the sidebar's own box; drawer-open and collapsed-list screenshots; nav items are tap-target checked.
- Tests (133 total): sidebar markup/order/current highlighting/escaping/script order (`test_app.py`); desktop navigation, collapse + persistence,
  live dots, phone drawer open/close/Esc/backdrop/theme/sign-out, resize-to-desktop (`test_e2e_browser.py`).
- Not changed: `preview/index.html` (standalone mock) still has the old layout.

## Overview page named "All clients" (2026-11-09, requested by the owner: "make it sync with All Clients")
Interpretation: the nav item, breadcrumb root and browser title all said "All clients" but the page itself was headed "AWS Environment Control".
Now the overview heading is **All clients** (small "AWS Environment Control" label above it), so nav item = page heading = breadcrumb root = tab title.
Also fixed the tally grammar for a single client: "1 of 1 environment running" (was "environments"). Tests: heading/nav/breadcrumb/title agree
(`test_app.py`, `test_e2e_browser.py`), and a one-client server checks the singular wording and that the sidebar count agrees. The browser-test server
fixture was refactored into `launch()` to allow that. The test watcher now ignores `net::ERR_ABORTED` (a navigation cancelling an in-flight load made one
test flaky); real failures and HTTP errors are still reported. Suite: 136 passed.

## Overview: label line removed (2026-11-09, requested by the owner)
Removed the small "AWS Environment Control" line above the "All clients" heading on the overview (and its dead `.kicker` CSS). The heading is now the
first line of the page; the product name remains in the browser tab title and on the sign-in page. Tests updated. Suite: 136 passed.

## Page's first line aligned with the sidebar "All clients" item (2026-11-09, owner's clarification)
"Sync" meant vertical alignment, not wording: the page heading should sit level with the sidebar's "All clients" row. It only did so by accident while the
label line existed above the heading; removing that line broke it. Now it is deliberate: the sidebar geometry is named CSS values (`--side-pad`,
`--side-logo`, `--nav-gap`, `--nav-row` -> `--nav-center`, plus `--h1-size`) and on desktop (>=992px) the overview's `h1` and the client page's breadcrumb row are
centred on `--nav-center`. Measured identical (0.0px difference) at 1000/1280/1500/1920px on both pages. Tablet/phone use the top bar, so nothing to align.
Tests: `test_page_heading_is_level_with_the_all_clients_menu_item` (992/1280/1920, tolerance 1.5px). Also made `test_drawer_closes_when_resized_to_desktop`
wait for the asynchronous resize event instead of racing it (it failed once in a full run, passed in isolation). Suite: 139 passed on two consecutive runs.

## Region chip right-aligned (2026-11-09, requested by the owner)
On tablet/desktop (>=768px) the region chip is pinned to the right edge of the title row (`margin-left:auto`), level with the right edge of the
environment banner and vertically centred on the heading. On phones it stays beside the name (wrapping left-aligned under a long name) so it never floats alone on
the right. CSS only (`.intro-title h1{min-width:0}` lets very long names shrink instead of pushing the chip). New test: chip right edge == banner right edge at
1280px (+/-1.5px), vertically centred on the h1, and left-aligned without sideways scroll at 390px. Audit clean in both themes.

## "All Clients" in title case (2026-11-09, requested by the owner)
Renamed the page from "All clients" to **All Clients** everywhere it appears so the names stay in sync: overview heading (accent word is now "Clients"),
sidebar item, client-page breadcrumb root, overview tab title, the logo link's accessible name and the 404 page link. Tests updated; no screenshots were taken
or viewed for this change (owner asked to pause screenshotting). Suite: 140 passed.

## Client name heading in the accent orange (2026-11-09, requested by the owner)
The client-page heading (e.g. "Uniservice") now uses the same accent colour as the "Clients" word on the overview (`class="accent"` on `#client-name`). It follows the
theme: brand amber `#F7920A` on dark, deeper `#D97706` on light (keeps >=3:1 for large text on white). Verified by computed colours in both themes and by the
contrast audit, no screenshots (paused at the owner's request). Suite: 141 passed.

## Repo hygiene re-check (2026-11-09, requested by the owner)
Audited what git would pick up: 53 files, no images/logs/archives/keys/OS junk, nothing ignored that the app needs. Findings and changes:
- `.gitignore`: added `.envrc`, `secrets/`, local databases (`*.sqlite`, `*.sqlite3`, `*.db`; likely once an audit DB is added), `.cache/`, and merge leftovers
  (`*.orig`, `*.rej`). Verified each new rule ignores a sample path and that `.env.example`, `clients.example.json`, `iam/*`, fonts, `preview/` etc. stay tracked.
- New `.gitattributes`: `* text=auto eol=lf` plus binary markers (woff2 etc.). This machine has `core.autocrlf=true` but production is Linux, where a CRLF in the `Procfile`
  breaks the start command. Files on disk were already LF, so no churn (dry-run `git add`: 0 warnings).
- Still open: `PROGRESS.md` contains the real AWS account ID, profile name and instance/database IDs from the live test. Not credentials, but internal infrastructure detail;
  redact or keep the file out of any shared/public remote.
- Note: `aws-control-panel-design/preview/index.html` (521 KB) is committed but stale (old light-only UI, its own inlined CSS).

## Real AWS end-to-end test (2026-11-09, account 290475839868, profile `uni_test_app`, region ap-south-2)
Scope agreed with the owner: only `i-0870b78c59dec7641` (uniservice_test_app-ec2) and `uniservice-test-db-rds`.
`integra_test_app-ec2` (`i-0cc062b174254b6a6`) was deliberately NOT in `clients.json` and stayed `running` throughout.
Run with `AWS_PROFILE=uni_test_app AUTH_MODE=dev AWS_MODE=real`; each step cross-checked with the AWS CLI.

| Test | Result |
|---|---|
| Live status read vs AWS CLI | match (database `available`, app `running`) |
| Single EC2 stop via API | 202, then `stopped` in ~20s; CLI agrees |
| Duplicate stop while stopping | 409 "already stopping" |
| Non-configured instance (integra) | 404 "not set up for this client"; never touched |
| Single EC2 start via API | 202, `pending` -> `running` in ~5s; CLI agrees |
| **Environment stop** | app stopped 18:19:51, DB stop began 18:19:56 (after the app), DB `stopped` 18:24:16, op `done` 18:24:22 (~5 min). CLI agrees |
| **Environment start** | DB `starting` -> `rebooting` -> `configuring-enhanced-monitoring` -> `available` (18:30:25, ~6 min); app started only after that (18:30:30), `running` 18:30:36, op `done` 18:30:41. CLI agrees |
| Final state | everything `running`/`available`, same as before the test |

Findings:
- Real RDS passes through states not in the fake backend (`rebooting`, `configuring-enhanced-monitoring`). The classify fallback
  treats them as "starting", so the wait kept waiting correctly.
- The `TIMEOUT` printed at the end of the first run was a bug in `scripts/real_aws_check.py` (it waited for RDS to be
  `running`, but RDS says `available`), not an app bug. Fixed.
- Not tested against real AWS: IAM-denied path (the `-deployer` user is broad), cross-account `role_arn`/`external_id`,
  and the UI in a browser against real AWS (API-level only).

## Starting state (discovered, not assumed)
- The project root `aws-control-panel/` contained only `aws-control-panel-design/` (the frontend). No backend code. (A
  first attempt at the start of the session was discarded when the folders were reorganised; everything here was rebuilt
  from scratch in the root folder.)
- Git: branch `main`, **no commits**, design folder untracked. **I have not committed anything.**
- Python 3.13.3 / Windows 11. Created `.venv/`; installed flask, gunicorn, boto3, flask-wtf, flask-talisman,
  flask-limiter, pytest, moto, playwright (+ Chromium) and pyflakes.
- gunicorn does not run on Windows (needs `fcntl`), so local runs use Werkzeug; gunicorn is for Railway only.

## Design decisions
| Decision | Choice | Reason |
|---|---|---|
| App shape | Factory `create_app()`; gunicorn `"app:create_app()"` | Testable, no import-time side effects |
| Frontend | `template_folder` / `static_folder` -> `aws-control-panel-design/` | One source of truth |
| Files | `app.py` `api.py` `auth.py` `config.py` `aws_ops.py` `operations.py` | Auth isolated behind `auth.py` so SSO is a drop-in |
| AWS | `RealBackend` (boto3) + `FakeBackend` behind one interface | Tests never touch AWS; `AWS_MODE=fake` lets you try the UI |
| Operations | In-process daemon threads, 1 gunicorn worker | Simple; state lost on restart (documented) |
| Status cache | 3s TTL per client+kind; operations bypass it; writes invalidate it | UI polls every 4-15s per tab |
| Dev auth | `AUTH_MODE=dev`; startup refuses it and `AWS_MODE=fake` in production | Cannot ship by accident |
| `AUTH_MODE=google` | Startup error "not implemented, Phase 2" | No half-built SSO |

## Backlog (all verified)
- [x] 1. Skeleton: `app.py`, `requirements*.txt`, `Procfile`, `/healthz`, ProxyFix, secure cookies, env config, `.env.example`, `.gitignore`
- [x] 2. Clients config: `config.py` schema validation (slug/region/ARN/ID formats, duplicates, at least one resource); 22 tests
- [x] 3. Auth seam + dev stub: `auth.py` (`login_required`, `current_user`, `/login`, `/login/google`, `/logout`); stub refuses production; new session on login
- [x] 4. CSRF: Flask-WTF, `X-CSRF-Token` header; `csrf_token()` in templates; JSON 400 for API, redirect for forms
- [x] 5. Page routes: `/` (overview), `/c/<slug>` (dashboard), 404 page; templates unchanged
- [x] 6. `aws_ops.py`: boto3 describe/start/stop, per-client role assumption with renewal, error mapping, fake backend; 26 tests (moto)
- [x] 7. Read API: `/api/overview`, `/api/c/<slug>/status`
- [x] 8. Write API: per-resource, per-kind, environment; 404/409/403 JSON `{ok:false,error}`
- [x] 9. Operations: multi-step, background thread, one per client, timeout/failure handling; 20+ tests
- [x] 10. Hardening: CSP (no inline script, no eval), HSTS, frame deny, `no-store`, rate limits (JSON 429), audit log, friendly AWS errors, request size cap
- [x] 11. Contract test: real `mock-api.js` in Chromium vs the backend, 18 scenarios
- [x] 12. Railway docs: `README.md`, `iam/*.json` (least-privilege role, tag-based variant, trust policy, panel-user policy)
- [x] 13. Gate: this document

## Log of steps, in order
1. Inspected the folder; found only the frontend. Read all templates/JS/CSS earlier in the session to learn the API contract
   (`common.js` header) and behaviour (`mock-api.js`).
2. Created venv and installed dependencies. Wrote skeleton files (requirements, Procfile, .gitignore, .env.example, clients.example.json copy).
3. Wrote `config.py` and `tests/test_config.py` -> 22 passed.
4. Wrote `aws_ops.py` and `tests/test_aws_ops.py` (real boto3 paths under moto, incl. assume-role + credential refresh,
   unknown IDs, denied, wrong state, missing creds; fake backend) -> 26 passed.
5. Wrote `operations.py` and `tests/test_operations.py`. **Bug found by test**: the HTTP response message for an
   environment start read `op.message` after the worker thread had already changed it (returned "The environment is
   running." instead of "Starting the databases."). Fixed by capturing the message before starting the thread. 69 passed, 3 runs stable.
6. Wrote `auth.py`, `api.py`, `app.py`. Smoke-tested with the Flask test client.
7. Wrote `tests/test_app.py` (auth, CSRF, contract, errors, headers, rate limits, injection-style IDs).
   **Bug found by test**: `.woff2` fonts were served as `application/octet-stream` (Windows registry lacks the type).
   Fixed with an explicit `mimetypes.add_type("font/woff2", ".woff2")`.
8. Wrote `tests/test_contract.py`: loads the real `mock-api.js` into headless Chromium and compares it to the backend (same
   client data) over 18 scenarios. 17 matched exactly; 1 difference was a mock quirk (see Deviations).
9. Wrote `tests/test_e2e_browser.py`: real UI in Chromium against the running server. **Bug found (frontend, see below)**: toasts never displayed.
   Then adjusted the tests to use Playwright `expect` (its `wait_for_function` evals a string, which my correct
   `script-src 'self'` CSP blocks; I did not weaken the CSP). Reviewed screenshots in `shots/`.
10. Added production-mode tests (HTTP->HTTPS redirect, healthcheck exempt, HSTS, Secure cookie behind the proxy).
11. Lint (pyflakes) clean. Wrote `README.md` and `iam/*.json` (validated as JSON).
12. Three consecutive full runs: 116 passed each.

## Frontend changes (the only one)
`aws-control-panel-design/static/css/style.css`: added `.toasts .toast{display:flex}`.
Why: Bootstrap's `.toast:not(.show){display:none}` out-ranks the design's `.toast{display:flex}`, so **no toast was ever
visible** (every success/error message hidden). Found by the browser test (`display:none` on the toast element).

## Deviations from the mock (intentional)
| Case | Mock | Real | Why |
|---|---|---|---|
| Stop all apps while they're already stopping | "Every application is already stopped." | "Some applications are still changing state..." (409) | The mock's wording is wrong; same status and keys |
| Environment start/stop when already in that state | creates a no-op operation | 409 "Everything is already running/stopped." | The UI disables those buttons anyway |
| `/api/overview` resource rows | `{state}` | `{state, error}` | `classify()` needs the error to flag broken resources |
| Operation finish | done once the last step is *requested* | done once the last group has *settled* | The "environment is running/stopped" toast should be true |
| Operation retention | forever | done 2 min, failed 10 min | Failed ops would otherwise show forever |

## Open questions / things for the owner
1. **Not committed.** No commits exist on `main`. Say the word and I will make the first commit (design + backend).
2. **Pre-existing design-CSS collisions with Bootstrap** (same class as the toast bug). The toast was fixed because it broke a
   feature. Left alone: Bootstrap's `.row` (the design reuses the name for resource rows) adds ~12px negative margins and
   child padding, so on mobile the row status pills sit slightly closer to the card edge than intended. Cosmetic.
3. **Real AWS has not been exercised** (no credentials available here). The boto3 code paths are tested against moto, including
   role assumption, but a first test against a real client account is needed once you have the IAM user/role set up.
4. **Not deployed.** Production startup intentionally fails until Phase 2 (SSO). gunicorn itself could not be run on Windows.
5. Decide when needed: Postgres for a durable audit log / multi-replica (currently in-memory, single worker).

## Phase 2 (SSO), not started
Waiting for "APPROVED FOR SSO". Plan: `GoogleProvider` (Authlib) behind the existing `auth.py` seam, state+nonce validation,
`hd` + `email_verified` + email-suffix domain check, tests with a mocked IdP (wrong domain, unverified email, bad state,
replayed callback), remove dev stub from production builds, go-live docs.
