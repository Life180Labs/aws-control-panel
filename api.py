"""JSON API. The contract is documented at the top of static/js/common.js:

  GET  /api/overview
  GET  /api/c/<slug>/status
  POST /api/c/<slug>/environment/{start|stop}
  POST /api/c/<slug>/{ec2|rds}/{start|stop}
  POST /api/c/<slug>/{ec2|rds}/<id>/{start|stop}
  -> 200/202 {ok: true, message} | 4xx/5xx {ok: false, error}
"""
from flask import Blueprint, current_app, jsonify

from auth import actor, login_required

bp = Blueprint("api", __name__, url_prefix="/api")


def svc():
    return current_app.extensions["l180_service"]


def accepted(message: str):
    return jsonify(ok=True, message=message), 202


@bp.get("/overview")
@login_required
def overview():
    return jsonify(svc().overview())


@bp.get("/c/<slug>/status")
@login_required
def status(slug):
    return jsonify(svc().status(slug))


@bp.post("/c/<slug>/environment/<any(start,stop):action>")
@login_required
def environment(slug, action):
    return accepted(svc().environment_action(slug, action, actor()))


@bp.post("/c/<slug>/<any(rds,ec2):kind>/<any(start,stop):action>")
@login_required
def kind_action(slug, kind, action):
    return accepted(svc().resource_action(slug, kind, None, action, actor()))


@bp.post("/c/<slug>/<any(rds,ec2):kind>/<rid>/<any(start,stop):action>")
@login_required
def resource_action(slug, kind, rid, action):
    return accepted(svc().resource_action(slug, kind, rid, action, actor()))
