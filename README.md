# AWS Environment Control

Internal L180 Labs tool: start and stop each client's AWS environment (RDS databases and EC2 applications) from a web UI
so the client only pays for what they use. Flask backend + the UI in `aws-control-panel-design/`.

**Status: features and Google SSO are implemented and tested.** Local development uses a one-click sign-in stub; production uses
Google sign-in restricted to your company domain and refuses to start with anything else. A real Google login has not been
exercised yet (it needs your Google OAuth credentials): see "Google sign-in" below to switch it on.

## Run it locally (no AWS, no SSO needed)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env          # local settings: read automatically in development (git-ignored; never read in production)
$env:AUTH_MODE="dev"; $env:AWS_MODE="fake"
.venv\Scripts\python app.py     # http://127.0.0.1:5000
```

Click **Sign in with Google**: in dev mode this signs you in as a local user. `AWS_MODE=fake` serves pretend resources
(the first client running, the rest stopped) whose start/stop take a few seconds, like the preview. Make it faster with
`FAKE_SPEED=0.25`.

Clients come from `clients.json` (git-ignored) or `CLIENTS_JSON`; in dev the app falls back to `clients.example.json`.

**Local settings file.** In development the app reads `.env` (copy `.env.example`) for any setting below. Real environment variables
always win over it, and it is ignored in production (Railway) and in the tests, so a `.env` can never change a deployment. It must be
UTF-8 text (PowerShell's `>` writes UTF-16: save it from an editor). `.env` is git-ignored; never commit it.

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
| `test_app.py` | auth, CSRF, API contract, error shapes, security headers, rate limits, navigation markup |
| `test_sso.py` | Google sign-in against a local OpenID Connect provider (`fake_idp.py`) that signs real RS256 tokens: domain rules, forged/expired/wrong-audience/wrong-nonce tokens, tampered or replayed callbacks, provider outages, session fixation, rate limiting, config validation |
| `test_contract.py` | loads the real `preview/mock-api.js` in Chromium and compares its answers to the backend's |
| `test_e2e_browser.py` | the real UI in Chromium against the running app: journeys, sidebar and mobile drawer, theme, a full Google sign-in round trip, session expiry; fails on any console/CSP/HTTP error |
| `test_ui_layout.py` | responsive layout and WCAG text contrast at phone/tablet/desktop in both themes |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_MODE` | `dev` locally, `google` in production | `dev` = one-click stub (refused in production). `google` = Google sign-in |
| `AWS_MODE` | `real` | `fake` = pretend AWS for local use (refused in production) |
| `SECRET_KEY` | random in dev | Signs session cookies. **Required in production** |
| `ALLOWED_DOMAIN` | `life180labs.com` | Only Google Workspace accounts of this domain may sign in |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | | OAuth client from Google Cloud (required when `AUTH_MODE=google`) |
| `PUBLIC_BASE_URL` | request URL | Public address, e.g. `https://panel.example.com`. Fixes the OAuth redirect address. **Required (https) in production** |
| `OIDC_DISCOVERY_URL` | Google's | Only change for testing against another OpenID Connect provider |
| `CLIENTS_JSON` / `CLIENTS_FILE` | `clients.json` | Client definitions (see `clients.example.json`) |
| `RAILWAY_ENVIRONMENT` / `PRODUCTION` | unset | Either one switches on production mode (HTTPS, secure cookies, safety refusals) |
| `STATUS_CACHE_TTL` | `3` | Seconds a status read may be cached per client |
| `OP_POLL_INTERVAL` / `OP_WAIT_TIMEOUT` | `5` / `1800` | How often and how long an operation waits for AWS |
| `RATE_LIMIT_READ` / `RATE_LIMIT_WRITE` / `RATE_LIMIT_LOGIN` | `240` / `30` / `20 per minute` | Per signed-in user (sign-in endpoints: per client IP) |
| `SESSION_HOURS` | `12` | How long a sign-in lasts |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | | The control-panel IAM user (boto3 standard) |

`clients.json` entry: `slug`, `name`, `region`, optional `role_arn` (+ `external_id`) for a client in another AWS account,
`rds_instances` and `ec2_instances` as `{id, name}` lists. The browser only ever sends the slug and a resource id; the
server rejects anything not listed here (404).

## Google sign-in

**What the app enforces.** The sign-in uses the OpenID Connect authorization-code flow with `state`, `nonce` and PKCE. The
ID token's signature, issuer, audience and expiry are validated. A person gets in only if **all three** hold: Google says
their email is verified, their account belongs to the `ALLOWED_DOMAIN` Workspace (the `hd` claim), and their email address
is exactly `name@ALLOWED_DOMAIN` (subdomains and look-alike domains are refused). Personal Gmail accounts have no `hd` claim,
so they are refused. The session is replaced on login, and every refusal sends the user back to the sign-in page with a fixed
message (nothing from Google is displayed).

### One-time setup in Google Cloud

1. Create or choose a project (Google Cloud console). **APIs & Services > OAuth consent screen**: choose user type
   **Internal** (available because L180 uses Google Workspace; it limits sign-in to your organisation even before the
   app's own check). Add the scopes `openid`, `email`, `profile`.
2. **APIs & Services > Credentials > Create credentials > OAuth client ID**, application type **Web application**.
   Under **Authorized redirect URIs** add exactly:
   * `https://<your-railway-domain>/auth/callback`  (production)
   * `http://localhost:5000/auth/callback`  (only if you want to try real Google locally)
3. Copy the **Client ID** and **Client secret**. Treat the secret like a password (Railway variables only, never the repo).

### Try real Google sign-in locally

Put these in `.env` (git-ignored), with the OAuth client's redirect URI set to `http://localhost:5000/auth/callback`:

```
AUTH_MODE=google
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
PUBLIC_BASE_URL=http://localhost:5000
AWS_MODE=fake            # pretend AWS; use real with AWS_PROFILE=<profile> to use real test resources
```

```powershell
.venv\Scripts\python app.py        # open http://localhost:5000  (not 127.0.0.1: the cookie host must match the redirect URI)
```

### Go-live checklist (Railway)

1. Variables: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_BASE_URL` (`https://` + the exact domain used in the redirect
   URI), `ALLOWED_DOMAIN`, `SECRET_KEY` (long random), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `CLIENTS_JSON`.
   Do **not** set `AUTH_MODE` (production defaults to `google`) and never set `AWS_MODE=fake`.
2. Deploy; the app refuses to start and says why if anything above is missing or unsafe. Check `/healthz`.
3. Sign in with a company account; try a personal Gmail account and confirm it is refused.
4. One replica only (operations and rate limits live in memory).

### Good to know

* A sign-in lasts `SESSION_HOURS` (12). Removing someone from Workspace does not end a session already open; to sign everyone
  out immediately, change `SECRET_KEY`.
* Every company user who can sign in can start and stop every configured client: there are no roles yet.
* The audit log records sign-ins and refusals (`event=login`, `event=login_denied reason=...`) with the email address, never tokens.

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
* **Uptime since the last start.** Each running resource shows "Uptime - 3h 12m" (hover for the exact start time) and the environment
  banner shows "Fully up for ..." (how long everything has been up together). The API rows carry `started_at`, `uptime_seconds` and
  `uptime_over_seconds`. **EC2** uses the instance's `LaunchTime`, which AWS resets on every stop/start. **RDS** has no start time on the
  instance, so it uses the newest `DB instance started` event (or the creation event if it was never stopped; reboots do not count).
  RDS keeps events for only 14 days, so a database up for longer shows "Uptime - over 14 days". This needs the `rds:DescribeEvents`
  permission (included in `iam/*.json`); without it the uptime is simply hidden and nothing else is affected.

## AWS permissions (least privilege)

1. In each client account create role **`l180-control-panel`** with `iam/client-role-policy.json` (fill in region, account and
   resource IDs) and trust policy `iam/client-role-trust.json`. Prefer the tag-based variant if you tag resources
   `l180-control-panel=managed`.
2. In the control-panel AWS account create IAM user `l180-control-panel` with `iam/panel-user-policy.json` and put its
   access keys into Railway variables. Put each client's role ARN (and `external_id`) in `clients.json`.
3. A client in the control-panel account itself can omit `role_arn`; grant the user the client-role policy directly.

If a call is denied the UI shows "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."

## Deploying to Railway

1. New Railway project from this repo; root directory = this folder. Railway detects Python from `requirements.txt` and
   starts the `Procfile` command. Set the healthcheck path to `/healthz`.
2. Set the variables from the go-live checklist above. Railway sets `RAILWAY_ENVIRONMENT` itself, which turns on production
   mode (HTTPS redirect, HSTS, secure cookies, and the refusals for the dev stub and fake AWS).
3. Keep it to **one replica**.
4. `clients.json` is git-ignored; supply the clients through the `CLIENTS_JSON` variable.

## Known limitations

* In-memory operations and rate limits (single process). Add Postgres if a durable audit trail or multiple replicas are needed.
* No roles: every signed-in company user has the same access.
* `gunicorn` does not run on Windows; use `python app.py` locally.
