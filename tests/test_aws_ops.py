
import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_ops import (DENIED_MESSAGE, NOT_FOUND_MESSAGE, FakeBackend, RealBackend, ServiceError, classify, friendly,
                     is_transition)
from config import Client, Resource, parse_clients

REGION = "ap-south-1"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("state,err,key", [
    ("running", None, "ok"), ("available", None, "ok"), ("stopped", None, "stopped"),
    ("pending", None, "starting"), ("starting", None, "starting"), ("stopping", None, "stopping"),
    ("shutting-down", None, "stopping"), ("", None, "unknown"), (None, None, "unknown"),
    ("terminated", None, "error"), ("failed", None, "error"), ("storage-full", None, "error"),
    ("rebooting", None, "starting"), ("backing-up", None, "starting"), ("running", "boom", "error"),
])
def test_classify_matches_frontend(state, err, key):
    assert classify(state, err) == key


def test_is_transition():
    assert is_transition("starting") and is_transition("stopping") and not is_transition("ok")


@pytest.fixture
def world():
    """moto account with 2 EC2 instances and 1 RDS instance."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ids = [i["InstanceId"] for i in ec2.run_instances(ImageId="ami-12345678", MinCount=2, MaxCount=2)["Instances"]]
        rds = boto3.client("rds", region_name=REGION)
        rds.create_db_instance(DBInstanceIdentifier="acme-db", DBInstanceClass="db.t3.micro", Engine="postgres",
                               MasterUsername="u", MasterUserPassword="password123", AllocatedStorage=20)
        client = Client("acme", "Acme", REGION, rds=(Resource("acme-db", "DB"),),
                        ec2=tuple(Resource(i, f"App {n}") for n, i in enumerate(ids)))
        yield client, ec2, rds, ids


def test_real_describe_start_stop(world):
    client, ec2, rds, ids = world
    b = RealBackend()
    assert [r["state"] for r in b.describe(client, "ec2")] == ["running", "running"]
    (db,) = b.describe(client, "rds")
    assert (db["id"], db["state"], db["error"]) == ("acme-db", "available", None)
    assert set(db) == {"id", "state", "error", "started_at", "up_over_seconds"}        # uptime fields are always present

    b.stop(client, "ec2", ids)
    b.stop(client, "rds", ["acme-db"])
    assert {r["state"] for r in b.describe(client, "ec2")} <= {"stopping", "stopped"}
    assert b.describe(client, "rds")[0]["state"] in ("stopping", "stopped")
    ec2.get_waiter("instance_stopped").wait(InstanceIds=ids)

    b.start(client, "ec2", ids[:1])
    states = {r["id"]: r["state"] for r in b.describe(client, "ec2")}
    assert states[ids[0]] in ("pending", "running") and states[ids[1]] == "stopped"


def test_real_unknown_resource_reports_error_per_row(world):
    client, _, _, ids = world
    ghost = Client("acme", "Acme", REGION, rds=(Resource("ghost-db", "Ghost"), Resource("acme-db", "DB")),
                   ec2=(Resource(ids[0], "A"), Resource("i-0000000000000dead", "Ghost")))
    b = RealBackend()
    ec2 = {r["id"]: r for r in b.describe(ghost, "ec2")}
    assert ec2[ids[0]]["state"] == "running" and ec2[ids[0]]["error"] is None
    assert ec2["i-0000000000000dead"]["error"] == NOT_FOUND_MESSAGE
    rds = {r["id"]: r for r in b.describe(ghost, "rds")}
    assert rds["ghost-db"]["error"] == NOT_FOUND_MESSAGE and rds["acme-db"]["state"] == "available"


def test_real_wrong_state_maps_to_409(world):
    client, _, _, _ = world
    b = RealBackend()
    b.stop(client, "rds", ["acme-db"])
    with pytest.raises(ServiceError) as e:
        b.stop(client, "rds", ["acme-db"])
    assert e.value.status == 409


def test_real_assumes_role_when_configured(world):
    _, _, _, ids = world
    arn = "arn:aws:iam::123456789012:role/l180-control-panel"
    c = Client("other", "Other", REGION, ec2=(Resource(ids[0], "A"),), role_arn=arn, external_id="ext")
    b = RealBackend()
    assert b.describe(c, "ec2")[0]["state"] == "running"
    api1 = b._api(c, "ec2")
    assert b._api(c, "ec2") is api1                       # cached
    b._now = lambda: 10**12                                 # past expiry -> re-assumed
    assert b._api(c, "ec2") is not api1


def test_friendly_error_mapping():
    def ce(code):
        return ClientError({"Error": {"Code": code, "Message": "secret internal detail"}}, "Op")
    assert friendly(ce("UnauthorizedOperation")) == (DENIED_MESSAGE, 403)
    assert friendly(ce("AccessDenied"))[1] == 403
    assert friendly(ce("InvalidDBInstanceState"))[1] == 409
    assert friendly(ce("Throttling"))[1] == 503
    msg, status = friendly(ce("SomethingNew"))
    assert status == 502 and "secret" not in msg


def test_real_access_denied_is_reported_per_resource_and_on_write(monkeypatch, world):
    client, _, _, ids = world
    b = RealBackend()

    class Denied:
        def describe_instances(self, **kw): raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "x")
        def stop_instances(self, **kw): raise ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "x")
    monkeypatch.setattr(b, "_api", lambda c, s: Denied())
    rows = b.describe(client, "ec2")
    assert all(r["error"] == DENIED_MESSAGE and r["state"] == "unknown" for r in rows)
    with pytest.raises(ServiceError) as e:
        b.stop(client, "ec2", ids)
    assert e.value.status == 403 and e.value.message == DENIED_MESSAGE


def test_missing_credentials_message(monkeypatch, world):
    client, *_ = world
    from botocore.exceptions import NoCredentialsError
    b = RealBackend()
    monkeypatch.setattr(b, "_api", lambda c, s: (_ for _ in ()).throw(NoCredentialsError()))
    assert "credentials" in b.describe(client, "rds")[0]["error"]


# ---------- fake backend ----------

class Clock:
    t = 0.0
    def __call__(self): return self.t


@pytest.fixture
def fake():
    cs = parse_clients({"clients": [
        {"slug": "a", "name": "A", "region": REGION, "rds_instances": [{"id": "a-db"}], "ec2_instances": [{"id": "i-0123456789abcdef0"}]},
        {"slug": "b", "name": "B", "region": REGION, "ec2_instances": [{"id": "i-0fedcba9876543210"}]}]})
    clock = Clock()
    return FakeBackend(cs, clock=clock), cs, clock


def test_fake_defaults_first_client_running(fake):
    f, (a, b), _ = fake
    assert f.describe(a, "rds")[0]["state"] == "available"
    assert f.describe(b, "ec2")[0]["state"] == "stopped"


def test_fake_transitions_complete_after_delay(fake):
    f, (a, b), clock = fake
    f.stop(a, "ec2", ["i-0123456789abcdef0"])
    assert f.describe(a, "ec2")[0]["state"] == "stopping"
    clock.t = 5.9
    assert f.describe(a, "ec2")[0]["state"] == "stopping"
    clock.t = 6.1
    assert f.describe(a, "ec2")[0]["state"] == "stopped"
    f.start(a, "ec2", ["i-0123456789abcdef0"])
    assert f.describe(a, "ec2")[0]["state"] == "pending"
    clock.t = 20
    assert f.describe(a, "ec2")[0]["state"] == "running"


def test_fake_rejects_wrong_state_and_can_fail_next(fake):
    f, (a, _), _ = fake
    with pytest.raises(ServiceError) as e:
        f.start(a, "rds", ["a-db"])
    assert e.value.status == 409
    f.fail_next = ServiceError(DENIED_MESSAGE, 403)
    with pytest.raises(ServiceError):
        f.stop(a, "rds", ["a-db"])
    f.stop(a, "rds", ["a-db"])   # hook is one-shot
