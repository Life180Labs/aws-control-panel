"""Business logic between the HTTP API and the AWS backend.

* status / overview reads (with a short cache so polling does not hammer AWS)
* single-resource and per-kind start/stop with the same rules as preview/mock-api.js
* environment start/stop: a multi-step operation that runs in a background thread
    start: databases -> wait until available -> applications
    stop : applications -> wait until stopped -> databases
  Only one operation runs per client. State is in memory (one process); it is lost on restart.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from aws_ops import Conflict, NotFound, ServiceError, classify, is_transition
from config import Client

log = logging.getLogger("operations")
audit = logging.getLogger("audit")

WORDS = {
    "rds": {"one": "database", "many": "databases", "up": "available"},
    "ec2": {"one": "application", "many": "applications", "up": "running"},
}


class Operation:
    def __init__(self, op_type: str, steps: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.type = op_type
        self.status = "running"          # running | done | failed
        self.steps = steps
        self.step = 0
        self.message = ""
        self.finished_at: float | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "status": self.status,
                "steps": list(self.steps), "step": self.step, "message": self.message}


class _Abort(Exception):
    """Raised inside the worker to fail the operation with a user-facing message."""


class EnvironmentService:
    def __init__(self, clients: list[Client], backend, *, cache_ttl=3.0, poll_interval=5.0, wait_timeout=1800.0,
                 done_retention=120.0, failed_retention=600.0, clock=time.monotonic, sleep=time.sleep, wall=time.time):
        self.clients = {c.slug: c for c in clients}
        self.order = [c.slug for c in clients]
        self.backend = backend
        self.cache_ttl, self.poll_interval, self.wait_timeout = cache_ttl, poll_interval, wait_timeout
        self.done_retention, self.failed_retention = done_retention, failed_retention
        self._clock, self._sleep, self._wall = clock, sleep, wall   # wall = real time, for uptime (clock is monotonic)
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._cache_locks = {s: threading.Lock() for s in self.clients}
        self._action_locks = {s: threading.RLock() for s in self.clients}
        self._ops: dict[str, Operation] = {}
        self._threads: list[threading.Thread] = []

    # ---------- reads ----------

    def get_client(self, slug: str) -> Client:
        c = self.clients.get(slug)
        if not c:
            raise NotFound("Unknown client.")
        return c

    def rows(self, client: Client, kind: str, fresh: bool = False) -> list[dict]:
        """[{id, name, state, error}] for one kind, in configured order."""
        key = (client.slug, kind)
        with self._cache_locks[client.slug]:
            hit = self._cache.get(key)
            if not fresh and hit and self._clock() - hit[0] < self.cache_ttl:
                data = hit[1]
            else:
                data = self.backend.describe(client, kind)
                self._cache[key] = (self._clock(), data)
        names = {r.id: r.name for r in client.resources(kind)}
        now = self._wall()
        rows = []
        for d in data:
            running = classify(d["state"], d["error"]) == "ok"
            started = d.get("started_at") if running else None
            rows.append({
                "id": d["id"], "name": names.get(d["id"], d["id"]), "state": d["state"], "error": d["error"],
                # uptime is computed now from the (cached) start time, so the 3s status cache never makes it stale
                "started_at": datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
                "uptime_seconds": int(max(0.0, now - started)) if started else None,
                # RDS keeps only 14 days of events: a database up for longer reports a lower bound instead of a start
                "uptime_over_seconds": d.get("up_over_seconds") if running and not started else None,
            })
        return rows

    def _invalidate(self, client: Client, kind: str | None = None):
        with self._cache_locks[client.slug]:
            for k in ([kind] if kind else ["rds", "ec2"]):
                self._cache.pop((client.slug, k), None)

    def operation(self, slug: str) -> dict | None:
        op = self._ops.get(slug)
        if not op:
            return None
        if op.finished_at is not None:
            keep = self.done_retention if op.status == "done" else self.failed_retention
            if self._clock() - op.finished_at > keep:
                self._ops.pop(slug, None)
                return None
        return op.to_dict()

    def _running(self, slug: str) -> bool:
        op = self._ops.get(slug)
        return bool(op and op.status == "running")

    def status(self, slug: str) -> dict:
        c = self.get_client(slug)
        return {"client": {"slug": c.slug, "name": c.name}, "region": c.region,
                "rds": self.rows(c, "rds"), "ec2": self.rows(c, "ec2"), "operation": self.operation(slug)}

    def overview(self) -> dict:
        def one(slug):
            c = self.clients[slug]
            strip = lambda rows: [{"state": r["state"], "error": r["error"]} for r in rows]  # noqa: E731
            return {"slug": c.slug, "name": c.name, "region": c.region, "operation": self.operation(slug),
                    "rds": strip(self.rows(c, "rds")), "ec2": strip(self.rows(c, "ec2"))}
        with ThreadPoolExecutor(max_workers=min(8, len(self.order))) as pool:
            return {"clients": list(pool.map(one, self.order))}

    # ---------- single resource / per-kind actions ----------

    def resource_action(self, slug: str, kind: str, rid: str | None, action: str, actor: str = "") -> str:
        c = self.get_client(slug)
        w = WORDS[kind]
        if rid is not None and not c.resource(kind, rid):
            raise NotFound("That resource is not set up for this client.")
        if not c.resources(kind):
            raise NotFound(f"This client has no {w['many']}.")
        with self._action_locks[slug]:
            if self._running(slug):
                raise Conflict("Another operation is already in progress for this client.")
            rows = self.rows(c, kind, fresh=True)
            want_from = "stopped" if action == "start" else "ok"       # key a resource must be in to act on it
            done_key = "ok" if action == "start" else "stopped"

            if rid is not None:
                row = next(r for r in rows if r["id"] == rid)
                key = classify(row["state"], row["error"])
                if is_transition(key):
                    raise Conflict(f"{row['name']} is already {'stopping' if key == 'stopping' else 'starting'}.")
                if key == done_key:
                    raise Conflict(f"{row['name']} is already {w['up'] if key == 'ok' else 'stopped'}.")
                targets, label = [rid], row["name"]       # error/unknown rows: let AWS decide
                message = f"{'Starting' if action == 'start' else 'Stopping'} {label}."
            else:
                keys = {r["id"]: classify(r["state"], r["error"]) for r in rows}
                targets = [i for i, k in keys.items() if k == want_from]
                if not targets:
                    if any(is_transition(k) for k in keys.values()):
                        raise Conflict(f"Some {w['many']} are still changing state. Try again in a moment.")
                    raise Conflict(f"Every {w['one']} is already {w['up'] if action == 'start' else 'stopped'}."
                                   if len(keys) > 1 else f"The {w['one']} is already {w['up'] if action == 'start' else 'stopped'}.")
                n = len(targets)
                message = f"{'Starting' if action == 'start' else 'Stopping'} {n} {w['one'] if n == 1 else w['many']}."

            try:
                (self.backend.start if action == "start" else self.backend.stop)(c, kind, targets)
            finally:
                self._invalidate(c, kind)
            audit.info("actor=%s client=%s target=%s/%s action=%s result=accepted",
                       actor, slug, kind, rid or "*", action)
            return message

    # ---------- whole environment ----------

    def environment_action(self, slug: str, action: str, actor: str = "") -> str:
        c = self.get_client(slug)
        with self._action_locks[slug]:
            if self._running(slug):
                raise Conflict("Another operation is already in progress for this client.")
            keys = {k: [classify(r["state"], r["error"]) for r in self.rows(c, k, fresh=True)] for k in ("rds", "ec2")}
            flat = keys["rds"] + keys["ec2"]
            if any(is_transition(k) for k in flat):
                raise Conflict("Some resources are still starting or stopping. Try again in a moment.")
            done = "ok" if action == "start" else "stopped"
            if all(k == done for k in flat):
                raise Conflict(f"Everything is already {'running' if action == 'start' else 'stopped'}.")

            has_db, has_app = bool(c.rds), bool(c.ec2)
            plan = self._plan(c, action, has_db, has_app)
            op = Operation(action, [p[0] for p in plan])
            op.message = plan[0][2]
            plan[0][1]()    # first step runs inside the request so errors (e.g. IAM denied) reach the user at once
            self._ops[slug] = op
            audit.info("actor=%s client=%s target=environment action=%s result=accepted steps=%d",
                       actor, slug, action, len(plan))
            t = threading.Thread(target=self._run, args=(c, op, plan, action, actor), daemon=True,
                                 name=f"op-{slug}-{action}")
            self._threads.append(t)
            message = plan[0][2]          # captured first: the worker updates op.message as soon as it starts
            t.start()
            return message

    def _plan(self, c: Client, action: str, has_db: bool, has_app: bool):
        """[(label, callable, message)]; the first callable runs synchronously, the rest in the worker."""
        steps = []
        if action == "start":
            if has_db:
                steps.append(("Start databases", lambda: self._do(c, "rds", "start"), "Starting the databases."))
            if has_db and has_app:
                steps.append(("Wait until available", lambda: self._wait(c, "rds", "ok"),
                              "Waiting for the databases to become available."))
            if has_app:
                steps.append(("Start applications", lambda: self._do(c, "ec2", "start"), "Starting the applications."))
        else:
            if has_app:
                steps.append(("Stop applications", lambda: self._do(c, "ec2", "stop"), "Stopping the applications."))
            if has_db and has_app:
                steps.append(("Wait until stopped", lambda: self._wait(c, "ec2", "stopped"),
                              "Waiting for the applications to stop."))
            if has_db:
                steps.append(("Stop databases", lambda: self._do(c, "rds", "stop"), "Stopping the databases."))
        return steps

    def _do(self, c: Client, kind: str, action: str):
        """Start/stop every resource of a kind that is in the right state; ones already moving are left alone."""
        rows = self.rows(c, kind, fresh=True)
        src = "stopped" if action == "start" else "ok"
        ids = [r["id"] for r in rows if classify(r["state"], r["error"]) == src]
        try:
            if ids:
                (self.backend.start if action == "start" else self.backend.stop)(c, kind, ids)
        finally:
            self._invalidate(c, kind)

    def _wait(self, c: Client, kind: str, goal: str):
        deadline = self._clock() + self.wait_timeout
        w = WORDS[kind]
        while True:
            rows = self.rows(c, kind, fresh=True)
            keys = [classify(r["state"], r["error"]) for r in rows]
            bad = next((r for r, k in zip(rows, keys) if k == "error"), None)
            if bad:
                raise _Abort(f"{bad['name']} reported an error.")
            if all(k == goal for k in keys):
                return
            if self._clock() >= deadline:
                raise _Abort(f"Timed out waiting for the {w['many']} to become {'available' if goal == 'ok' else 'stopped'}.")
            self._sleep(self.poll_interval)

    def _run(self, c: Client, op: Operation, plan, action: str, actor: str):
        try:
            for i, (_, fn, message) in enumerate(plan):
                op.step, op.message = i, message
                if i > 0:                                  # step 0 already ran in the request
                    fn()
            last = "rds" if (action == "start" and c.rds and not c.ec2) or (action == "stop" and c.rds) else "ec2"
            op.message = "Waiting for everything to finish starting." if action == "start" else "Waiting for everything to finish stopping."
            self._wait(c, last, "ok" if action == "start" else "stopped")
            op.step = len(plan)
            op.message = "The environment is running." if action == "start" else "The environment is stopped."
            op.status = "done"
            audit.info("actor=%s client=%s target=environment action=%s result=done", actor, c.slug, action)
        except _Abort as e:
            self._fail(c, op, action, actor, str(e))
        except ServiceError as e:
            self._fail(c, op, action, actor, e.message)
        except Exception:
            log.exception("operation %s/%s crashed", c.slug, action)
            self._fail(c, op, action, actor, "Something went wrong. Check the resources and try again.")
        finally:
            op.finished_at = self._clock()
            self._invalidate(c)

    def _fail(self, c, op, action, actor, reason):
        op.status = "failed"
        op.message = f"Couldn't {action} the environment. {reason}"
        audit.warning("actor=%s client=%s target=environment action=%s result=failed reason=%s", actor, c.slug, action, reason)

    def join(self, timeout=10.0):
        """Wait for background operations (tests)."""
        for t in list(self._threads):
            t.join(timeout)
