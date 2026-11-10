import threading

import pytest

from aws_ops import Conflict, FakeBackend, NotFound, ServiceError
from config import parse_clients
from operations import EnvironmentService

REGION = "ap-south-1"
APP1, APP2, APP3 = "i-0123456789abcdef0", "i-0fedcba9876543210", "i-0aaaabbbbccccdddd"
DENIED = "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy."

CLIENTS = parse_clients({"clients": [
    {"slug": "full", "name": "Full", "region": REGION,
     "rds_instances": [{"id": "full-db", "name": "Primary"}, {"id": "full-rep", "name": "Reporting"}],
     "ec2_instances": [{"id": APP1, "name": "Web"}, {"id": APP2, "name": "API"}]},
    {"slug": "nodb", "name": "No DB", "region": REGION, "ec2_instances": [{"id": APP3, "name": "Store"}]},
    {"slug": "dbonly", "name": "DB only", "region": REGION, "rds_instances": [{"id": "only-db"}]},
]})


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(running=("full", "nodb", "dbonly"), gate=None, timeout=1800.0):
    clock = Clock()
    backend = FakeBackend(CLIENTS, clock=clock, running=set(running))

    def sleep(dt):
        if gate:
            gate.wait(5)
        clock.t += dt
    svc = EnvironmentService(CLIENTS, backend, cache_ttl=0, poll_interval=5, wait_timeout=timeout, clock=clock, sleep=sleep)
    return svc, backend, clock


def states(svc, slug, kind):
    return [r["state"] for r in svc.rows(svc.get_client(slug), kind, fresh=True)]


# ---------- reads ----------

def test_status_shape_matches_contract():
    svc, *_ = make()
    s = svc.status("full")
    assert s["client"] == {"slug": "full", "name": "Full"} and s["region"] == REGION
    row = s["rds"][0]
    assert {k: row[k] for k in ("id", "name", "state", "error")} == {"id": "full-db", "name": "Primary", "state": "available", "error": None}
    assert set(row) == {"id", "name", "state", "error", "started_at", "uptime_seconds", "uptime_over_seconds"}   # the full contract
    assert row["uptime_seconds"] is not None and row["started_at"].endswith("Z")                              # a running DB has an uptime
    assert s["ec2"][1]["name"] == "API" and s["operation"] is None


def test_overview_shape_and_unknown_client():
    svc, *_ = make()
    ov = svc.overview()
    assert [c["slug"] for c in ov["clients"]] == ["full", "nodb", "dbonly"]
    assert ov["clients"][0]["rds"] == [{"state": "available", "error": None}] * 2
    assert ov["clients"][1]["rds"] == []
    with pytest.raises(NotFound):
        svc.status("nope")


def test_status_cache_hits_backend_once_within_ttl():
    clock = Clock()
    backend = FakeBackend(CLIENTS, clock=clock)
    n = {"c": 0}
    real = backend.describe
    backend.describe = lambda c, k: (n.__setitem__("c", n["c"] + 1), real(c, k))[1]
    svc = EnvironmentService(CLIENTS, backend, cache_ttl=3, clock=clock)
    svc.status("full")
    svc.status("full")
    assert n["c"] == 2          # rds + ec2 once each
    clock.t = 4
    svc.status("full")
    assert n["c"] == 4


# ---------- single / per-kind ----------

def test_single_resource_start_stop_messages_and_conflicts():
    svc, b, clock = make()
    assert svc.resource_action("full", "ec2", APP1, "stop") == "Stopping Web."
    assert states(svc, "full", "ec2")[0] == "stopping"
    with pytest.raises(Conflict, match="Web is already stopping"):
        svc.resource_action("full", "ec2", APP1, "stop")
    with pytest.raises(Conflict, match="API is already running"):
        svc.resource_action("full", "ec2", APP2, "start")
    clock.t = 10
    assert svc.resource_action("full", "ec2", APP1, "start") == "Starting Web."
    clock.t = 30
    with pytest.raises(Conflict, match="Primary is already available"):
        svc.resource_action("full", "rds", "full-db", "start")
    svc.resource_action("full", "rds", "full-db", "stop")
    clock.t = 60
    with pytest.raises(Conflict, match="Primary is already stopped"):
        svc.resource_action("full", "rds", "full-db", "stop")


def test_unknown_ids_and_wrong_kind_are_404():
    svc, *_ = make()
    with pytest.raises(NotFound):
        svc.resource_action("full", "ec2", "i-0000000000000000f", "stop")   # valid format, not configured
    with pytest.raises(NotFound):
        svc.resource_action("full", "ec2", "full-db", "stop")                # RDS id used as EC2
    with pytest.raises(NotFound, match="no databases"):
        svc.resource_action("nodb", "rds", None, "stop")
    with pytest.raises(NotFound):
        svc.resource_action("ghost", "ec2", None, "stop")


def test_kind_level_only_touches_eligible_and_reports_count():
    svc, b, clock = make()
    svc.resource_action("full", "ec2", APP1, "stop")
    clock.t = 10
    assert svc.resource_action("full", "ec2", None, "stop") == "Stopping 1 application."
    assert b.calls[-1][3] == [APP2]
    clock.t = 30
    assert svc.resource_action("full", "ec2", None, "start") == "Starting 2 applications."
    clock.t = 60
    with pytest.raises(Conflict, match="Every application is already running"):
        svc.resource_action("full", "ec2", None, "start")


def test_kind_level_conflict_when_all_moving():
    svc, b, clock = make()
    svc.resource_action("full", "ec2", None, "stop")
    with pytest.raises(Conflict, match="still changing state"):
        svc.resource_action("full", "ec2", None, "stop")


def test_backend_failure_propagates_with_status_and_does_not_poison_cache():
    svc, b, _ = make()
    b.fail_next = ServiceError(DENIED, 403)
    with pytest.raises(ServiceError) as e:
        svc.resource_action("full", "ec2", APP1, "stop")
    assert e.value.status == 403
    assert states(svc, "full", "ec2") == ["running", "running"]
    svc.resource_action("full", "ec2", APP1, "stop")   # works afterwards


# ---------- environment operations ----------

def test_environment_start_orders_databases_before_apps():
    svc, b, clock = make(running=())
    msg = svc.environment_action("full", "start", "tester")
    assert msg == "Starting the databases."
    svc.join()
    op = svc.operation("full")
    assert op["status"] == "done" and op["step"] == len(op["steps"]) == 3
    assert op["steps"] == ["Start databases", "Wait until available", "Start applications"]
    assert op["message"] == "The environment is running."
    db_starts = [c for c in b.calls if c[2] == "rds"]
    app_starts = [c for c in b.calls if c[2] == "ec2"]
    assert db_starts[0][3] == ["full-db", "full-rep"]
    assert app_starts[0][4] >= db_starts[0][4] + 9      # apps only after the DBs became available (9s fake delay)
    assert states(svc, "full", "rds") == ["available"] * 2 and states(svc, "full", "ec2") == ["running"] * 2


def test_environment_stop_orders_apps_before_databases():
    svc, b, clock = make()
    assert svc.environment_action("full", "stop") == "Stopping the applications."
    svc.join()
    op = svc.operation("full")
    assert op["status"] == "done" and op["steps"] == ["Stop applications", "Wait until stopped", "Stop databases"]
    assert op["message"] == "The environment is stopped."
    app_stop = next(c for c in b.calls if c[2] == "ec2")
    db_stop = next(c for c in b.calls if c[2] == "rds")
    assert db_stop[4] >= app_stop[4] + 6                 # databases only after apps fully stopped
    assert states(svc, "full", "rds") == ["stopped"] * 2 and states(svc, "full", "ec2") == ["stopped"] * 2


def test_client_without_database_has_single_step():
    svc, b, _ = make()
    svc.environment_action("nodb", "stop")
    svc.join()
    op = svc.operation("nodb")
    assert op["steps"] == ["Stop applications"] and op["status"] == "done"
    svc.environment_action("nodb", "start")
    svc.join()
    assert svc.operation("nodb")["steps"] == ["Start applications"]
    assert states(svc, "nodb", "ec2") == ["running"]


def test_database_only_client():
    svc, *_ = make()
    svc.environment_action("dbonly", "stop")
    svc.join()
    op = svc.operation("dbonly")
    assert op["steps"] == ["Stop databases"] and op["status"] == "done"
    assert states(svc, "dbonly", "rds") == ["stopped"]


def test_operation_visible_while_running_and_blocks_other_actions():
    gate = threading.Event()
    svc, b, _ = make(running=(), gate=gate)
    svc.environment_action("full", "start")
    op = svc.operation("full")
    assert op["status"] == "running" and op["type"] == "start" and op["step"] in (0, 1)
    with pytest.raises(Conflict, match="Another operation is already in progress"):
        svc.environment_action("full", "stop")
    with pytest.raises(Conflict, match="Another operation is already in progress"):
        svc.resource_action("full", "ec2", APP1, "start")
    gate.set()
    svc.join()
    assert svc.operation("full")["status"] == "done"


def test_other_clients_are_unaffected_by_a_running_operation():
    gate = threading.Event()
    svc, *_ = make(gate=gate)
    svc.environment_action("full", "stop")
    svc.environment_action("nodb", "stop")           # different client: allowed
    gate.set()
    svc.join()
    assert svc.operation("nodb")["status"] == "done"


def test_environment_conflicts():
    svc, b, _ = make()
    with pytest.raises(Conflict, match="already running"):
        svc.environment_action("full", "start")
    svc2, *_ = make(running=())
    with pytest.raises(Conflict, match="already stopped"):
        svc2.environment_action("full", "stop")
    svc3, b3, _ = make()
    svc3.resource_action("full", "ec2", APP1, "stop")
    with pytest.raises(Conflict, match="still starting or stopping"):
        svc3.environment_action("full", "start")


def test_first_step_failure_surfaces_immediately_and_creates_no_operation():
    svc, b, _ = make(running=())
    b.fail_next = ServiceError(DENIED, 403)
    with pytest.raises(ServiceError) as e:
        svc.environment_action("full", "start")
    assert e.value.status == 403 and svc.operation("full") is None
    svc.environment_action("full", "start")          # can retry
    svc.join()
    assert svc.operation("full")["status"] == "done"


def test_later_step_failure_marks_operation_failed_at_that_step():
    gate = threading.Event()
    svc, b, _ = make(running=(), gate=gate)
    svc.environment_action("full", "start")            # DBs start; worker blocked waiting
    b.fail_next = ServiceError("Boom from AWS.", 502)  # next backend call = starting the apps
    gate.set()
    svc.join()
    op = svc.operation("full")
    assert op["status"] == "failed" and op["step"] == 2
    assert "Boom from AWS." in op["message"] and op["message"].startswith("Couldn't start the environment.")
    svc.environment_action("full", "start")            # a failed op doesn't block a retry
    svc.join()
    assert svc.operation("full")["status"] == "done"


def test_wait_times_out():
    svc, b, clock = make(running=(), timeout=3)         # DBs need 9s but we only wait 3s
    svc.environment_action("full", "start")
    svc.join()
    op = svc.operation("full")
    assert op["status"] == "failed" and "Timed out" in op["message"] and op["step"] == 1


def test_resource_error_state_fails_wait():
    gate = threading.Event()
    svc, b, _ = make(running=(), gate=gate)
    svc.environment_action("full", "start")
    b.set_state("full", "rds", "full-db", "failed")
    gate.set()
    svc.join()
    op = svc.operation("full")
    assert op["status"] == "failed" and "Primary reported an error" in op["message"]


def test_operations_expire_after_retention():
    svc, b, clock = make()
    svc.done_retention = 100
    svc.environment_action("nodb", "stop")
    svc.join()
    assert svc.operation("nodb")["status"] == "done"
    clock.t += 101
    assert svc.operation("nodb") is None


def test_concurrent_environment_requests_start_only_one_operation():
    gate = threading.Event()
    svc, *_ = make(running=(), gate=gate)
    results = []

    def go():
        try:
            svc.environment_action("full", "start")
            results.append("ok")
        except Conflict:
            results.append("conflict")
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    gate.set()
    svc.join()
    assert sorted(results) == ["conflict"] * 5 + ["ok"]
