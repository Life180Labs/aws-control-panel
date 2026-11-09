import copy
import json
from pathlib import Path

import pytest

from config import ConfigError, Settings, parse_clients

GOOD = {"clients": [{
    "slug": "acme", "name": "Acme", "region": "ap-south-1",
    "rds_instances": [{"id": "acme-db", "name": "Primary"}],
    "ec2_instances": [{"id": "i-0123456789abcdef0"}],
}]}


def mutate(fn):
    d = copy.deepcopy(GOOD)
    fn(d["clients"][0])
    return d


def test_valid_config_parses():
    (c,) = parse_clients(GOOD)
    assert c.slug == "acme" and c.rds[0].name == "Primary"
    assert c.ec2[0].name == "i-0123456789abcdef0"   # name defaults to id
    assert c.resource("rds", "acme-db") and c.resource("rds", "nope") is None


def test_example_file_is_valid():
    example = str(Path(__file__).resolve().parent.parent / "clients.example.json")
    s = Settings.from_env({"CLIENTS_FILE": example})
    assert [c.slug for c in s.clients] == ["uniservice", "another-client"]


@pytest.mark.parametrize("fn,frag", [
    (lambda c: c.update(slug="Bad Slug"), "slug"),
    (lambda c: c.update(region="mars"), "region"),
    (lambda c: c.update(role_arn="not-an-arn"), "role_arn"),
    (lambda c: c.update(rds_instances=[{"id": "1bad"}]), "RDS identifier"),
    (lambda c: c.update(ec2_instances=[{"id": "web"}]), "EC2 identifier"),
    (lambda c: c.update(rds_instances=[], ec2_instances=[]), "at least one"),
    (lambda c: c.update(rds_instances=[{"id": "a"}, {"id": "a"}]), "duplicate"),
    (lambda c: c.pop("name"), "name"),
])
def test_invalid_client_rejected(fn, frag):
    with pytest.raises(ConfigError, match=frag):
        parse_clients(mutate(fn))


def test_duplicate_slug_and_empty():
    d = copy.deepcopy(GOOD); d["clients"].append(copy.deepcopy(d["clients"][0]))
    with pytest.raises(ConfigError, match="duplicate slug"):
        parse_clients(d)
    with pytest.raises(ConfigError):
        parse_clients({"clients": []})


def test_inline_json_wins_and_bad_json_fails():
    s = Settings.from_env({"CLIENTS_JSON": json.dumps(GOOD)})
    assert [c.slug for c in s.clients] == ["acme"]
    with pytest.raises(ConfigError, match="not valid JSON"):
        Settings.from_env({"CLIENTS_JSON": "{nope"})


def test_dev_defaults():
    s = Settings.from_env({})
    assert (s.auth_mode, s.aws_mode, s.production) == ("dev", "real", False)
    assert len(s.secret_key) >= 32


@pytest.mark.parametrize("env,frag", [
    ({"RAILWAY_ENVIRONMENT": "production", "AUTH_MODE": "dev", "SECRET_KEY": "x"}, "AUTH_MODE=dev"),
    ({"PRODUCTION": "1", "AUTH_MODE": "dev", "SECRET_KEY": "x"}, "AUTH_MODE=dev"),
    ({"RAILWAY_ENVIRONMENT": "production", "AWS_MODE": "fake", "AUTH_MODE": "google", "SECRET_KEY": "x"}, "AWS_MODE=fake"),
    ({"RAILWAY_ENVIRONMENT": "production", "SECRET_KEY": "x"}, "Phase 2"),   # production defaults to google
    ({"AUTH_MODE": "google"}, "Phase 2"),
    ({"AUTH_MODE": "bogus"}, "AUTH_MODE must be"),
    ({"AWS_MODE": "bogus"}, "AWS_MODE must be"),
    ({"STATUS_CACHE_TTL": "abc"}, "number"),
])
def test_unsafe_or_invalid_settings_refuse_to_start(env, frag):
    with pytest.raises(ConfigError, match=frag):
        Settings.from_env(env)


def test_production_requires_secret_and_ignores_example_file(monkeypatch):
    with pytest.raises(ConfigError):
        Settings.from_env({"PRODUCTION": "1", "AUTH_MODE": "google"})
