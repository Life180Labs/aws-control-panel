"""AWS access: one interface, two backends.

RealBackend - boto3 against EC2 / RDS, optionally assuming a per-client IAM role.
FakeBackend - in-memory resources with timed transitions (tests and AWS_MODE=fake demos).

Both: describe(client, kind) -> [{"id", "state", "error", "started_at", "up_over_seconds"}] in configured order;
      start / stop(client, kind, ids) -> None, raising ServiceError on failure.

`started_at` (epoch seconds, or None) is when a running/available resource was last started:
    EC2  the instance's LaunchTime, which AWS resets on every stop/start.
    RDS  the newest "DB instance started" event (or "DB instance created" if it was never stopped). RDS keeps events for at
         most 14 days: a database that has been up longer reports `up_over_seconds` instead ("up for over 14 days").
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

from config import Client

log = logging.getLogger("aws")

ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthFailure", "UnauthorizedAccess"}
WRONG_STATE = {"InvalidDBInstanceState", "InvalidDBInstanceStateFault", "IncorrectInstanceState", "IncorrectState"}
RDS_EVENT_WINDOW_DAYS = 14                 # the most RDS keeps (DescribeEvents Duration maximum: 20160 minutes)
RDS_START_CACHE_SECONDS = 300           # a start time only changes when the database is stopped and started again
NOT_FOUND = {"InvalidInstanceID.NotFound", "DBInstanceNotFound", "DBInstanceNotFoundFault", "InvalidInstanceID.Malformed"}
THROTTLED = {"Throttling", "ThrottlingException", "RequestLimitExceeded"}

DENIED_MESSAGE = "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."
CREDS_MESSAGE = "The dashboard can't sign in to AWS. Check its credentials."
NOT_FOUND_MESSAGE = "This resource wasn't found in AWS. Check the ID in the client configuration."
WRONG_STATE_MESSAGE = "That can't be done while the resource is in its current state."
GENERIC_MESSAGE = "AWS returned an error. Try again in a moment."


class ServiceError(Exception):
    """An error with an HTTP status and a message that is safe to show to users."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message, self.status = message, status


class NotFound(ServiceError):
    def __init__(self, message="Not found."):
        super().__init__(message, 404)


class Conflict(ServiceError):
    def __init__(self, message):
        super().__init__(message, 409)


# ---------- state classification (Python twin of classify() in static/js/common.js) ----------

_ERROR_RE = re.compile(r"fail|incompatible|inaccessible|storage-full|terminated|error")


def classify(state: str | None, error: str | None = None) -> str:
    """ok | stopped | starting | stopping | error | unknown"""
    if error:
        return "error"
    s = (state or "").lower()
    if s in ("running", "available"):
        return "ok"
    if s == "stopped":
        return "stopped"
    if s in ("pending", "starting"):
        return "starting"
    if s in ("stopping", "shutting-down"):
        return "stopping"
    if not s or s == "unknown":
        return "unknown"
    if _ERROR_RE.search(s):
        return "error"
    return "starting"  # other transitional DB states: rebooting, modifying, backing-up...


def is_transition(key: str) -> bool:
    return key in ("starting", "stopping")


def friendly(exc: Exception) -> tuple[str, int]:
    """Exception -> (message safe to show, HTTP status). Raw AWS details only go to the log."""
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError

    if isinstance(exc, ServiceError):
        return exc.message, exc.status
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ACCESS_DENIED:
            return DENIED_MESSAGE, 403
        if code in WRONG_STATE:
            return WRONG_STATE_MESSAGE, 409
        if code in NOT_FOUND:
            return NOT_FOUND_MESSAGE, 404
        if code in THROTTLED:
            return "AWS is rate limiting requests. Try again in a moment.", 503
        return GENERIC_MESSAGE, 502
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return CREDS_MESSAGE, 502
    if isinstance(exc, BotoCoreError):
        return "Couldn't reach AWS. Try again in a moment.", 502
    return GENERIC_MESSAGE, 502


# ---------- real backend ----------


class RealBackend:
    ROLE_SESSION = "l180-control-panel"

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._apis: dict[tuple[str, str], tuple[object, float]] = {}
        self._rds_start: dict[tuple[str, str], tuple[float, float | None, int | None]] = {}   # (slug, id) -> (fetched, started, over)

    def _api(self, client: Client, service: str):
        """boto3 client for this tenant. Role credentials are cached and renewed 5 min before expiry.
        A fresh Session is built per (re)creation because Sessions are not thread safe; clients are."""
        import boto3
        from botocore.config import Config

        key = (client.slug, service)
        with self._lock:
            cached = self._apis.get(key)
            if cached and cached[1] > self._now() + 300:
                return cached[0]
            cfg = Config(region_name=client.region, connect_timeout=5, read_timeout=15,
                         retries={"max_attempts": 3, "mode": "standard"})
            expires = float("inf")
            if client.role_arn:
                args = {"RoleArn": client.role_arn, "RoleSessionName": self.ROLE_SESSION, "DurationSeconds": 3600}
                if client.external_id:
                    args["ExternalId"] = client.external_id
                creds = boto3.client("sts", config=cfg).assume_role(**args)["Credentials"]
                api = boto3.session.Session().client(
                    service, config=cfg, aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
                exp = creds.get("Expiration")
                expires = exp.timestamp() if isinstance(exp, datetime) else self._now() + 3600
            else:
                api = boto3.session.Session().client(service, config=cfg)
            self._apis[key] = (api, expires)
            return api

    # -- reads --

    def describe(self, client: Client, kind: str) -> list[dict]:
        ids = [r.id for r in client.resources(kind)]
        try:
            found = self._describe_ec2(client, ids) if kind == "ec2" else self._describe_rds(client, ids)
        except Exception as exc:  # whole-call failure: credentials, role assumption, access denied
            log.warning("describe %s/%s failed: %s", client.slug, kind, exc)
            msg, _ = friendly(exc)
            return [{"id": i, "state": "unknown", "error": msg} for i in ids]
        return [found.get(i) or {"id": i, "state": "unknown", "error": NOT_FOUND_MESSAGE} for i in ids]

    def _describe_ec2(self, client, ids):
        from botocore.exceptions import ClientError

        api, out = self._api(client, "ec2"), {}

        def call(batch):
            for res in api.describe_instances(InstanceIds=batch).get("Reservations", []):
                for inst in res.get("Instances", []):
                    state, launched = inst["State"]["Name"], inst.get("LaunchTime")
                    out[inst["InstanceId"]] = {
                        "id": inst["InstanceId"], "state": state, "error": None, "up_over_seconds": None,
                        "started_at": launched.timestamp() if state == "running" and isinstance(launched, datetime) else None}

        try:
            call(ids)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in NOT_FOUND:
                raise
            for i in ids:  # one unknown ID fails the whole call: retry one by one
                try:
                    call([i])
                except ClientError as e2:
                    if e2.response.get("Error", {}).get("Code") not in NOT_FOUND:
                        out[i] = {"id": i, "state": "unknown", "error": friendly(e2)[0]}
        return out

    def _describe_rds(self, client, ids):
        from botocore.exceptions import ClientError

        api, out = self._api(client, "rds"), {}
        for i in ids:
            try:
                dbs = api.describe_db_instances(DBInstanceIdentifier=i).get("DBInstances", [])
                if dbs:
                    state, started, over = dbs[0]["DBInstanceStatus"], None, None
                    if state == "available":
                        started, over = self._rds_started(api, client, i)
                    else:
                        self._rds_start.pop((client.slug, i), None)      # leaving "available" invalidates the cached start
                    out[i] = {"id": i, "state": state, "error": None, "started_at": started, "up_over_seconds": over}
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ACCESS_DENIED:
                    raise  # same for every resource: report once
                out[i] = {"id": i, "state": "unknown", "error": friendly(e)[0]}
        return out

    def _rds_started(self, api, client: Client, rid: str) -> tuple[float | None, int | None]:
        """(started_at, up_over_seconds) for an available database, from RDS events. Never raises: uptime is a nicety.

        Only "DB instance started" counts as a start: "DB instance restarted" events also fire during every startup and on
        reboots, and would make a database look newer than it is. A database that was never stopped started when it was created.
        """
        key, now = (client.slug, rid), self._now()
        hit = self._rds_start.get(key)
        if hit and now - hit[0] < RDS_START_CACHE_SECONDS:
            return hit[1], hit[2]
        started = created = None
        over = None
        ttl_offset = 0.0
        try:
            pages = api.get_paginator("describe_events").paginate(
                SourceIdentifier=rid, SourceType="db-instance", Duration=RDS_EVENT_WINDOW_DAYS * 24 * 60)
            for page in pages:
                for ev in page.get("Events", []):
                    when, msg = ev.get("Date"), ev.get("Message", "")
                    if not isinstance(when, datetime):
                        continue
                    if msg == "DB instance started":
                        started = max(started or 0.0, when.timestamp())
                    elif msg == "DB instance created":
                        created = when.timestamp()
            if started is None and created is not None:
                started = created
            elif started is None:
                over = RDS_EVENT_WINDOW_DAYS * 86400             # nothing in the window: it has been up for longer
        except Exception as exc:                                  # e.g. rds:DescribeEvents not allowed, throttling
            log.warning("could not read RDS events for %s/%s (uptime hidden): %s", client.slug, rid, exc)
            started, over = None, None
            ttl_offset = RDS_START_CACHE_SECONDS - 60             # retry sooner than a success, but not on every poll
        self._rds_start[key] = (now - ttl_offset, started, over)
        return started, over

    # -- writes --

    def start(self, client: Client, kind: str, ids: list[str]) -> None:
        self._act(client, kind, ids, "start")

    def stop(self, client: Client, kind: str, ids: list[str]) -> None:
        self._act(client, kind, ids, "stop")

    def _act(self, client, kind, ids, action):
        if not ids:
            return
        try:
            if kind == "ec2":
                api = self._api(client, "ec2")
                (api.start_instances if action == "start" else api.stop_instances)(InstanceIds=ids)
                return
            api, first = self._api(client, "rds"), None
            for i in ids:
                try:
                    (api.start_db_instance if action == "start" else api.stop_db_instance)(DBInstanceIdentifier=i)
                except Exception as e:  # keep going so one bad DB doesn't block the rest
                    log.warning("%s %s/%s/%s failed: %s", action, client.slug, kind, i, e)
                    first = first or e
            if first:
                raise first
        except ServiceError:
            raise
        except Exception as exc:
            log.warning("%s %s/%s failed: %s", action, client.slug, kind, exc)
            msg, status = friendly(exc)
            raise ServiceError(msg, status) from None


# ---------- fake backend ----------


class FakeBackend:
    """Pretend AWS. Transitions finish after a delay like the preview mock (RDS 9s, EC2 6s, times `speed`).
    By default the first client is running and the others are stopped."""

    def __init__(self, clients: list[Client], speed: float = 1.0, clock=time.monotonic, running: set[str] | None = None,
                 wall=time.time, initial_uptime: float = 7500.0):
        self._clock, self._speed, self._wall = clock, speed, wall
        self._lock = threading.Lock()
        self.fail_next: ServiceError | None = None   # test hook
        self.calls: list[tuple] = []                  # test hook: (action, slug, kind, ids, clock time)
        if running is None:
            running = {clients[0].slug} if clients else set()
        self._rec: dict[tuple[str, str, str], dict] = {}
        for c in clients:
            for kind in ("rds", "ec2"):
                up = "available" if kind == "rds" else "running"
                for r in c.resources(kind):
                    is_up = c.slug in running
                    self._rec[(c.slug, kind, r.id)] = {"state": up if is_up else "stopped", "target": None, "at": 0.0,
                                                       "since": wall() - initial_uptime if is_up else None}

    def _settle(self, rec):
        if rec["target"] and self._clock() >= rec["at"]:
            rec["state"], rec["target"] = rec["target"], None
            # it became available `clock - at` seconds ago (describe may be called long after the transition finished)
            rec["since"] = self._wall() - (self._clock() - rec["at"]) if rec["state"] in ("available", "running") else None

    def describe(self, client, kind):
        with self._lock:
            out = []
            for r in client.resources(kind):
                rec = self._rec[(client.slug, kind, r.id)]
                self._settle(rec)
                is_up = rec["state"] in ("available", "running")
                out.append({"id": r.id, "state": rec["state"], "error": None,
                            "started_at": rec["since"] if is_up else None, "up_over_seconds": None})
            return out

    def _act(self, client, kind, ids, action):
        with self._lock:
            self.calls.append((action, client.slug, kind, list(ids), self._clock()))
            if self.fail_next:
                err, self.fail_next = self.fail_next, None
                raise err
            up = "available" if kind == "rds" else "running"
            for i in ids:
                rec = self._rec[(client.slug, kind, i)]
                self._settle(rec)
                if action == "start" and rec["state"] == "stopped":
                    rec.update(state="starting" if kind == "rds" else "pending", target=up)
                elif action == "stop" and rec["state"] == up:
                    rec.update(state="stopping", target="stopped")
                else:
                    raise ServiceError(WRONG_STATE_MESSAGE, 409)
                rec["at"] = self._clock() + (9.0 if kind == "rds" else 6.0) * self._speed

    def start(self, client, kind, ids):
        self._act(client, kind, ids, "start")

    def stop(self, client, kind, ids):
        self._act(client, kind, ids, "stop")

    def set_state(self, slug, kind, rid, state, error=None):   # test hook
        with self._lock:
            self._rec[(slug, kind, rid)].update(
                state=state, target=None, since=self._wall() if state in ("available", "running") else None)
