# AWS Environment Control

Internal L180 Labs tool: start and stop each client's AWS environment (RDS databases and EC2 applications) from a web UI
so the client only pays for what they use. Flask backend + the UI in `aws-control-panel-design/`.

**Status: Phase 1 (features) complete. Google SSO is Phase 2.** Until SSO lands the app runs only with a development
sign-in stub and **will refuse to start in production** (on Railway). See "Deploying to Railway".

## Run it locally (no AWS, no SSO needed)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env          # optional: values are read from real environment variables
$env:AUTH_MODE="dev"; $env:AWS_MODE="fake"
.venv\Scripts\python app.py     # http://127.0.0.1:5000
```

Click **Sign in with Google**: in dev mode this signs you in as a local user. `AWS_MODE=fake` serves pretend resources
(the first client running, the rest stopped) whose start/stop take a few seconds, like the preview. Make it faster with
`FAKE_SPEED=0.25`.

Clients come from `clients.json` (git-ignored) or `CLIENTS_JSON`; in dev the app falls back to `clients.example.json`.

### Tests

```powershell
.venv\Scripts\python -m playwright install chromium     # once, for the browser tests
.venv\Scripts\python -m pytest tests -q
```

| File | What it proves |
|---|---|
| `test_config.py` | client schema validation; production refuses the dev stub / fake AWS / missing secret |
| `test_aws_ops.py` | real boto3 code against moto (describe/start/stop, role assumption + refresh, error mapping) and the fake backend |
| `test_operations.py` | step order (DB first/apps first), waits, timeouts, failures, one-op-per-client, races |
| `test_app.py` | auth, CSRF, API contract, error shapes, security headers, rate limits |
| `test_contract.py` | loads the real `preview/mock-api.js` in Chromium and compares its answers to the backend's |
| `test_e2e_browser.py` | the real UI in Chromium against the running app: full journey, mobile, session expiry; fails on any console/CSP/HTTP error |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_MODE` | `dev` locally | `dev` = one-click stub (never in production). `google` arrives in Phase 2 |
| `AWS_MODE` | `real` | `fake` = pretend AWS for local use (never in production) |
| `SECRET_KEY` | random in dev | Signs session cookies. **Required in production** |
| `ALLOWED_DOMAIN` | `life180labs.com` | Shown on the sign-in page; enforced by SSO in Phase 2 |
| `CLIENTS_JSON` / `CLIENTS_FILE` | `clients.json` | Client definitions (see `clients.example.json`) |
| `RAILWAY_ENVIRONMENT` / `PRODUCTION` | unset | Either one switches on production mode (HTTPS, secure cookies, safety refusals) |
| `STATUS_CACHE_TTL` | `3` | Seconds a status read may be cached per client |
| `OP_POLL_INTERVAL` / `OP_WAIT_TIMEOUT` | `5` / `1800` | How often and how long an operation waits for AWS |
| `RATE_LIMIT_READ` / `RATE_LIMIT_WRITE` | `240 per minute` / `30 per minute` | Per signed-in user |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | | The control-panel IAM user (boto3 standard) |

`clients.json` entry: `slug`, `name`, `region`, optional `role_arn` (+ `external_id`) for a client in another AWS account,
`rds_instances` and `ec2_instances` as `{id, name}` lists. The browser only ever sends the slug and a resource id; the
server rejects anything not listed here (404).

## How it works

* `GET /api/overview`, `GET /api/c/<slug>/status`, `POST /api/c/<slug>/{environment|ec2|rds}/...` as documented in
  `aws-control-panel-design/static/js/common.js`.
* **Start environment**: databases start, wait until available, then applications start. **Stop**: applications stop,
  wait until stopped, then databases stop. It runs in a background thread; the first step runs inside the request so
  errors (for example IAM denied) reach the user immediately. One operation per client at a time (409 otherwise).
* States are raw AWS states; the UI and `aws_ops.classify()` map them identically.
* Operation state is in memory: a restart forgets a running operation (the resources keep doing what AWS is doing, and
  the next status read shows it). Hence **one gunicorn worker** (`--workers 1 --threads 8`).
* Every action writes an audit line: `actor=... client=... target=... action=... result=...`.

## AWS permissions (least privilege)

1. In each client account create role **`l180-control-panel`** with `iam/client-role-policy.json` (fill in region, account and
   resource IDs) and trust policy `iam/client-role-trust.json`. Prefer the tag-based variant if you tag resources
   `l180-control-panel=managed`.
2. In the control-panel AWS account create IAM user `l180-control-panel` with `iam/panel-user-policy.json` and put its
   access keys into Railway variables. Put each client's role ARN (and `external_id`) in `clients.json`.
3. A client in the control-panel account itself can omit `role_arn`; grant the user the client-role policy directly.

If a call is denied the UI shows "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."

## Deploying to Railway (after Phase 2)

1. New Railway project from this repo; root directory = this folder. Railway detects Python from `requirements.txt` and
   starts the `Procfile` command. Set the healthcheck path to `/healthz`.
2. Variables: `SECRET_KEY` (long random), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`,
   `CLIENTS_JSON` (or commit `clients.json` to a private repo), and the Google variables added in Phase 2.
   Railway sets `RAILWAY_ENVIRONMENT` itself, which turns on production mode.
3. Keep it to **one replica**.
4. **Today this deploy will intentionally fail at startup** ("AUTH_MODE=google ... not implemented yet") because the only
   auth available is the dev stub. That is the safety net, not a bug. It becomes deployable when Phase 2 lands.

## Known limitations

* No SSO yet (Phase 2). The dev stub exists only for local use.
* In-memory operations and rate limits (single process). Add Postgres if a durable audit trail or multiple replicas are needed.
* `gunicorn` does not run on Windows; use `python app.py` locally.
