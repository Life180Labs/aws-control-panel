"""Uptime since the last start: EC2 (LaunchTime), RDS (start events), the fake backend, and the service's computation."""
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_ops import RDS_EVENT_WINDOW_DAYS, RDS_START_CACHE_SECONDS, FakeBackend, RealBackend
from config import Client, Resource, parse_clients
from operations import EnvironmentService

REGION = "ap-south-1"
UTC = timezone.utc
T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def event(message, when, category="notification"):
    return {"Date": when, "Message": message, "EventCategories": [category], "SourceType": "db-instance"}


class FakeRdsApi:
    """Just enough of boto3's RDS client: describe_db_instances and a describe_events paginator (two pages)."""

    def __init__(self, status="available", events=(), fail=None):
        self.status, self.events, self.fail = status, list(events), fail
        self.event_calls, self.last_kwargs = 0, None

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceStatus": self.status, "InstanceCreateTime": T0 - timedelta(days=30)}]}

    def get_paginator(self, name):
        assert name == "describe_events"
        return self

    def paginate(self, **kwargs):
        self.event_calls += 1
        self.last_kwargs = kwargs
        if self.fail:
            raise self.fail
        half = len(self.events) // 2
        return iter([{"Events": self.events[:half]}, {"Events": self.events[half:]}])      # exercise pagination


@pytest.fixture
def db():
    client = Client("acme", "Acme", REGION, rds=(Resource("acme-db", "DB"),))
    clock = Clock()
    clock.t = T0.timestamp() + 3600
    backend = RealBackend(now=clock)
    api = FakeRdsApi()
    backend._api = lambda c, s: api
    return client, backend, api, clock


def describe(client, backend):
    return backend.describe(client, "rds")[0]


# ---------------------------------------------------------------- RDS: where the start time comes from

def test_rds_uses_the_newest_start_event_across_pages(db):
    client, backend, api, _ = db
    api.events = [event("DB instance started", T0), event("DB instance stopped", T0 + timedelta(hours=1)),
                  event("DB instance started", T0 + timedelta(hours=5)), event("Backing up DB instance", T0 + timedelta(hours=6), "backup")]
    row = describe(client, backend)
    assert row["state"] == "available" and row["error"] is None
    assert row["started_at"] == (T0 + timedelta(hours=5)).timestamp() and row["up_over_seconds"] is None


def test_rds_reboots_and_backups_are_not_starts(db):
    client, backend, api, _ = db
    api.events = [event("DB instance started", T0, "notification"),
                  event("DB instance restarted", T0 + timedelta(hours=9), "availability"),     # fires on every startup and reboot
                  event("DB instance shutdown", T0 + timedelta(hours=10), "availability"),
                  event("Finished DB Instance backup", T0 + timedelta(hours=11), "backup")]
    assert describe(client, backend)["started_at"] == T0.timestamp()


def test_rds_never_stopped_counts_from_creation(db):
    client, backend, api, _ = db
    api.events = [event("DB instance created", T0 - timedelta(days=3), "creation"), event("Backing up DB instance", T0, "backup")]
    assert describe(client, backend)["started_at"] == (T0 - timedelta(days=3)).timestamp()


def test_rds_start_event_wins_over_creation(db):
    client, backend, api, _ = db
    api.events = [event("DB instance created", T0 - timedelta(days=3), "creation"), event("DB instance started", T0)]
    assert describe(client, backend)["started_at"] == T0.timestamp()


def test_rds_up_longer_than_the_event_history_reports_a_lower_bound(db):
    client, backend, api, _ = db
    api.events = [event("Backing up DB instance", T0, "backup")]                 # nothing about starts in the last 14 days
    row = describe(client, backend)
    assert row["started_at"] is None and row["up_over_seconds"] == RDS_EVENT_WINDOW_DAYS * 86400


def test_rds_asks_for_the_whole_14_day_window_for_this_database_only(db):
    client, backend, api, _ = db
    describe(client, backend)
    assert api.last_kwargs == {"SourceIdentifier": "acme-db", "SourceType": "db-instance", "Duration": 14 * 24 * 60}


@pytest.mark.parametrize("failure", [
    ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed to rds:DescribeEvents"}}, "DescribeEvents"),
    ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "DescribeEvents"),
    RuntimeError("network blip"),
])
def test_rds_events_failing_hides_uptime_but_never_breaks_the_status(db, failure):
    client, backend, api, _ = db
    api.fail = failure
    row = describe(client, backend)
    assert row["state"] == "available" and row["error"] is None                  # the resource still reads as healthy
    assert row["started_at"] is None and row["up_over_seconds"] is None          # and we do not guess


def test_rds_start_time_is_cached_but_refreshed_and_failures_retry_sooner(db):
    client, backend, api, clock = db
    api.events = [event("DB instance started", T0)]
    describe(client, backend)
    describe(client, backend)
    assert api.event_calls == 1                                                   # status polling does not hammer RDS
    clock.t += RDS_START_CACHE_SECONDS + 1
    describe(client, backend)
    assert api.event_calls == 2

    api.fail, clock.t = RuntimeError("down"), clock.t + RDS_START_CACHE_SECONDS + 1
    describe(client, backend)
    assert api.event_calls == 3
    api.fail, api.events = None, [event("DB instance started", T0 + timedelta(hours=2))]
    clock.t += 30
    assert describe(client, backend)["started_at"] is None and api.event_calls == 3         # still inside the short retry window
    clock.t += 40                                                                 # > 60s after the failure
    assert describe(client, backend)["started_at"] == (T0 + timedelta(hours=2)).timestamp()


def test_rds_leaving_available_clears_the_cached_start(db):
    client, backend, api, _ = db
    api.events = [event("DB instance started", T0)]
    describe(client, backend)
    api.status = "stopped"
    stopped = describe(client, backend)
    assert stopped["state"] == "stopped" and stopped["started_at"] is None and stopped["up_over_seconds"] is None
    api.status, api.events = "available", [event("DB instance started", T0 + timedelta(hours=8))]
    row = describe(client, backend)                                               # started again: must not reuse the old time
    assert row["started_at"] == (T0 + timedelta(hours=8)).timestamp() and api.event_calls == 2


@pytest.mark.parametrize("status", ["starting", "stopping", "rebooting", "backing-up", "stopped", "failed"])
def test_rds_has_no_uptime_unless_available(db, status):
    client, backend, api, _ = db
    api.status = status
    row = describe(client, backend)
    assert row["started_at"] is None and api.event_calls == 0                    # and no needless events call


# ---------------------------------------------------------------- EC2: LaunchTime (real boto3 code against moto)

@pytest.fixture
def ec2_world(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ids = [i["InstanceId"] for i in ec2.run_instances(ImageId="ami-12345678", MinCount=2, MaxCount=2)["Instances"]]
        client = Client("acme", "Acme", REGION, ec2=tuple(Resource(i, f"App {n}") for n, i in enumerate(ids)))
        yield client, ec2, ids


def test_ec2_started_at_is_the_launch_time_and_only_while_running(ec2_world):
    client, ec2, ids = ec2_world
    launched = {i["InstanceId"]: i["LaunchTime"] for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]}
    backend = RealBackend()
    rows = {r["id"]: r for r in backend.describe(client, "ec2")}
    for i in ids:
        assert rows[i]["state"] == "running" and rows[i]["started_at"] == launched[i].timestamp() and rows[i]["up_over_seconds"] is None

    backend.stop(client, "ec2", [ids[0]])
    rows = {r["id"]: r for r in backend.describe(client, "ec2")}
    assert rows[ids[0]]["state"] in ("stopping", "stopped") and rows[ids[0]]["started_at"] is None     # not up: no uptime
    assert rows[ids[1]]["started_at"] == launched[ids[1]].timestamp()


# ---------------------------------------------------------------- fake backend

CLIENTS = parse_clients({"clients": [
    {"slug": "a", "name": "A", "region": REGION, "rds_instances": [{"id": "a-db"}], "ec2_instances": [{"id": "i-0123456789abcdef0"}]},
    {"slug": "b", "name": "B", "region": REGION, "ec2_instances": [{"id": "i-0fedcba9876543210"}]}]})


def fake(**kw):
    clock, wall = Clock(), Clock()
    wall.t = 1_000_000.0
    return FakeBackend(CLIENTS, clock=clock, wall=wall, **kw), clock, wall


def test_fake_running_resources_start_with_the_configured_uptime_and_stopped_ones_have_none():
    f, _, wall = fake(initial_uptime=3600)
    a, b = CLIENTS
    assert f.describe(a, "ec2")[0]["started_at"] == wall.t - 3600 and f.describe(a, "rds")[0]["started_at"] == wall.t - 3600
    assert f.describe(b, "ec2")[0]["state"] == "stopped" and f.describe(b, "ec2")[0]["started_at"] is None


def test_fake_restart_resets_the_start_time_to_when_it_became_available():
    f, clock, wall = fake()
    a, _ = CLIENTS
    f.stop(a, "ec2", ["i-0123456789abcdef0"])
    assert f.describe(a, "ec2")[0]["started_at"] is None                         # stopping: not up
    clock.t, wall.t = 10, wall.t + 10
    assert f.describe(a, "ec2")[0]["state"] == "stopped"
    f.start(a, "ec2", ["i-0123456789abcdef0"])
    assert f.describe(a, "ec2")[0]["started_at"] is None                         # pending: not up yet
    clock.t, wall.t = 100, wall.t + 90                                           # 94s after it finished starting (6s boot at t=16)
    row = f.describe(a, "ec2")[0]
    assert row["state"] == "running" and row["started_at"] == wall.t - (100 - 16)


# ---------------------------------------------------------------- service: what the API returns

def service(initial_uptime=7500.0):
    clock, wall = Clock(), Clock()
    wall.t = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC).timestamp()
    backend = FakeBackend(CLIENTS, clock=clock, wall=wall, initial_uptime=initial_uptime)
    return EnvironmentService(CLIENTS, backend, cache_ttl=3, clock=clock, wall=wall), backend, clock, wall


def test_status_rows_carry_started_at_and_uptime_seconds():
    svc, _, _, wall = service(initial_uptime=7500)
    row = svc.status("a")["ec2"][0]
    assert row["uptime_seconds"] == 7500 and row["started_at"] == "2026-11-10T09:55:00Z" and row["uptime_over_seconds"] is None
    stopped = svc.status("b")["ec2"][0]
    assert stopped["started_at"] is None and stopped["uptime_seconds"] is None and stopped["uptime_over_seconds"] is None


def test_uptime_keeps_counting_even_while_the_status_cache_serves_the_same_data():
    svc, _, clock, wall = service(initial_uptime=100)
    assert svc.status("a")["ec2"][0]["uptime_seconds"] == 100
    clock.t, wall.t = 2, wall.t + 2                                              # inside the 3s cache: no new AWS call...
    assert svc.status("a")["ec2"][0]["uptime_seconds"] == 102                    # ...but the uptime still moved on


def test_only_running_resources_show_uptime_not_errors_or_transitions():
    svc, backend, _, _ = service()
    backend.set_state("a", "ec2", "i-0123456789abcdef0", "pending")
    backend.set_state("a", "rds", "a-db", "failed")
    row_ec2, row_rds = svc.status("a")["ec2"][0], svc.status("a")["rds"][0]
    assert row_ec2["started_at"] is None and row_ec2["uptime_seconds"] is None
    assert row_rds["started_at"] is None and row_rds["uptime_seconds"] is None


def test_a_start_time_in_the_future_never_gives_negative_uptime():
    svc, _, _, _ = service(initial_uptime=-500)                                   # clock skew: "started" 500s from now
    assert svc.status("a")["ec2"][0]["uptime_seconds"] == 0


def test_uptime_over_is_passed_through_only_for_running_resources_without_a_known_start():
    class OverBackend:
        def describe(self, client, kind):
            return [{"id": r.id, "state": "available" if kind == "rds" else "stopped", "error": None,
                     "started_at": None, "up_over_seconds": 14 * 86400} for r in client.resources(kind)]
    svc = EnvironmentService(CLIENTS[:1], OverBackend(), cache_ttl=0)
    st = svc.status("a")
    assert st["rds"][0]["uptime_over_seconds"] == 14 * 86400 and st["rds"][0]["uptime_seconds"] is None
    assert st["ec2"][0]["uptime_over_seconds"] is None                           # stopped: nothing to report


def test_backends_that_do_not_report_uptime_still_work():
    class OldBackend:                                                            # e.g. an error row built without the new keys
        def describe(self, client, kind):
            return [{"id": r.id, "state": "running" if kind == "ec2" else "available", "error": None} for r in client.resources(kind)]
    st = EnvironmentService(CLIENTS[:1], OldBackend(), cache_ttl=0).status("a")
    assert st["ec2"][0]["started_at"] is None and st["ec2"][0]["uptime_seconds"] is None
