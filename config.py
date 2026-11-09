"""Settings and client configuration, read from environment variables (see .env.example).

Clients come from CLIENTS_JSON (inline), else CLIENTS_FILE, else clients.json; in development only,
clients.example.json is the final fallback.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent
DESIGN_DIR = BASE_DIR / "aws-control-panel-design"  # templates/ and static/ live here, untouched

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EC2_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
RDS_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")
ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")
REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")

KINDS = ("rds", "ec2")


class ConfigError(Exception):
    """Anything that makes the app unsafe or impossible to start."""


@dataclass(frozen=True)
class Resource:
    id: str
    name: str


@dataclass(frozen=True)
class Client:
    slug: str
    name: str
    region: str
    rds: tuple[Resource, ...] = ()
    ec2: tuple[Resource, ...] = ()
    role_arn: str | None = None
    external_id: str | None = None

    def resources(self, kind: str) -> tuple[Resource, ...]:
        if kind not in KINDS:
            raise KeyError(kind)
        return getattr(self, kind)

    def resource(self, kind: str, rid: str) -> Resource | None:
        return next((r for r in self.resources(kind) if r.id == rid), None)

    def public(self) -> dict:
        """The only shape templates see."""
        return {"slug": self.slug, "name": self.name, "region": self.region}


def _req_str(obj: dict, key: str, where: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{where}: '{key}' is required and must be a non-empty string.")
    return v.strip()


def _resources(items, id_re, field: str, where: str) -> tuple[Resource, ...]:
    if items is None:
        return ()
    if not isinstance(items, list):
        raise ConfigError(f"{where}: '{field}' must be a list.")
    out, seen = [], set()
    for i, it in enumerate(items):
        w = f"{where}.{field}[{i}]"
        if not isinstance(it, dict):
            raise ConfigError(f"{w}: must be an object with 'id' and 'name'.")
        rid = _req_str(it, "id", w)
        if not id_re.match(rid):
            raise ConfigError(f"{w}: '{rid}' is not a valid {field.split('_')[0].upper()} identifier.")
        if rid in seen:
            raise ConfigError(f"{w}: duplicate id '{rid}'.")
        seen.add(rid)
        name = it.get("name")
        out.append(Resource(rid, name.strip() if isinstance(name, str) and name.strip() else rid))
    return tuple(out)


def parse_clients(data) -> list[Client]:
    if not isinstance(data, dict) or not isinstance(data.get("clients"), list) or not data["clients"]:
        raise ConfigError("Clients config must be an object with a non-empty 'clients' list.")
    clients, slugs = [], set()
    for i, c in enumerate(data["clients"]):
        where = f"clients[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"{where}: must be an object.")
        slug = _req_str(c, "slug", where)
        if not SLUG_RE.match(slug):
            raise ConfigError(f"{where}: slug '{slug}' must be lowercase letters, digits and hyphens.")
        if slug in slugs:
            raise ConfigError(f"{where}: duplicate slug '{slug}'.")
        slugs.add(slug)
        where = f"client '{slug}'"
        region = _req_str(c, "region", where)
        if not REGION_RE.match(region):
            raise ConfigError(f"{where}: '{region}' does not look like an AWS region.")
        role = c.get("role_arn")
        if role is not None and (not isinstance(role, str) or not ROLE_ARN_RE.match(role)):
            raise ConfigError(f"{where}: 'role_arn' is not a valid IAM role ARN.")
        ext = c.get("external_id")
        if ext is not None and not isinstance(ext, str):
            raise ConfigError(f"{where}: 'external_id' must be a string.")
        rds = _resources(c.get("rds_instances"), RDS_ID_RE, "rds_instances", where)
        ec2 = _resources(c.get("ec2_instances"), EC2_ID_RE, "ec2_instances", where)
        if not rds and not ec2:
            raise ConfigError(f"{where}: needs at least one entry in rds_instances or ec2_instances.")
        clients.append(Client(slug, _req_str(c, "name", where), region, rds, ec2, role, ext))
    return clients


def load_clients(env: Mapping[str, str], production: bool) -> list[Client]:
    inline = env.get("CLIENTS_JSON")
    if inline:
        try:
            return parse_clients(json.loads(inline))
        except json.JSONDecodeError as e:
            raise ConfigError(f"CLIENTS_JSON is not valid JSON: {e}") from None
    paths = []
    if env.get("CLIENTS_FILE"):
        paths.append(Path(env["CLIENTS_FILE"]))
    paths.append(BASE_DIR / "clients.json")
    if not production:
        paths.append(BASE_DIR / "clients.example.json")
    for p in paths:
        if p.is_file():
            try:
                return parse_clients(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError as e:
                raise ConfigError(f"{p.name} is not valid JSON: {e}") from None
    raise ConfigError("No clients configured. Set CLIENTS_JSON or provide clients.json (see clients.example.json).")


def _num(env, key, default):
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got '{raw}'.") from None
    if v < 0:
        raise ConfigError(f"{key} must not be negative.")
    return v


def _flag(env, key, default=False):
    raw = env.get(key)
    return default if raw in (None, "") else raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    production: bool
    auth_mode: str
    aws_mode: str
    secret_key: str
    allowed_domain: str
    dev_user_name: str
    dev_user_email: str
    clients: list[Client]
    cache_ttl: float = 3.0
    poll_interval: float = 5.0
    wait_timeout: float = 1800.0
    done_retention: float = 120.0
    failed_retention: float = 600.0
    rate_read: str = "240 per minute"
    rate_write: str = "30 per minute"
    rate_limit_enabled: bool = True
    fake_speed: float = 1.0
    session_hours: float = 12.0
    trust_proxy: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        production = bool(env.get("RAILWAY_ENVIRONMENT")) or _flag(env, "PRODUCTION")
        auth_mode = (env.get("AUTH_MODE") or ("google" if production else "dev")).lower()
        aws_mode = (env.get("AWS_MODE") or "real").lower()

        if auth_mode not in ("dev", "google"):
            raise ConfigError(f"AUTH_MODE must be 'dev' or 'google', got '{auth_mode}'.")
        if aws_mode not in ("real", "fake"):
            raise ConfigError(f"AWS_MODE must be 'real' or 'fake', got '{aws_mode}'.")
        if production and auth_mode == "dev":
            raise ConfigError("Refusing to start: AUTH_MODE=dev is a development stub and cannot run in production.")
        if production and aws_mode == "fake":
            raise ConfigError("Refusing to start: AWS_MODE=fake serves pretend data and cannot run in production.")
        if auth_mode == "google":
            raise ConfigError("AUTH_MODE=google (SSO) is not implemented yet; it arrives in Phase 2.")

        secret = env.get("SECRET_KEY", "")
        if not secret:
            if production:
                raise ConfigError("SECRET_KEY is required in production.")
            secret = secrets.token_hex(32)  # dev only: sessions reset on every restart

        return cls(
            production=production, auth_mode=auth_mode, aws_mode=aws_mode, secret_key=secret,
            allowed_domain=env.get("ALLOWED_DOMAIN", "life180labs.com").strip(),
            dev_user_name=env.get("DEV_USER_NAME", "Local Admin"),
            dev_user_email=env.get("DEV_USER_EMAIL", "admin@example.com"),
            clients=load_clients(env, production),
            cache_ttl=_num(env, "STATUS_CACHE_TTL", 3.0),
            poll_interval=_num(env, "OP_POLL_INTERVAL", 5.0),
            wait_timeout=_num(env, "OP_WAIT_TIMEOUT", 1800.0),
            rate_read=env.get("RATE_LIMIT_READ", "240 per minute"),
            rate_write=env.get("RATE_LIMIT_WRITE", "30 per minute"),
            rate_limit_enabled=_flag(env, "RATE_LIMIT_ENABLED", True),
            fake_speed=_num(env, "FAKE_SPEED", 1.0),
            session_hours=_num(env, "SESSION_HOURS", 12.0),
            trust_proxy=production or _flag(env, "TRUST_PROXY"),
        )
