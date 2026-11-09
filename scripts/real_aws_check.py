"""Drive a RUNNING instance of the app over HTTP (dev sign-in) and print what it reports. For real-AWS checks.

    python scripts/real_aws_check.py http://127.0.0.1:5099 status
    python scripts/real_aws_check.py http://127.0.0.1:5099 post /api/c/uniservice/ec2/i-.../stop
    [KINDS=ec2] python scripts/real_aws_check.py http://127.0.0.1:5099 wait uniservice <state> [timeout_s]
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

base, cmd, *rest = sys.argv[1:]
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, path):
    req = urllib.request.Request(base + path, method=method, headers={"X-CSRF-Token": TOKEN} if method == "POST" else {})
    try:
        with op.open(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


op.open(base + "/login/google").read()
TOKEN = re.search(r'name="csrf-token" content="([^"]+)"', op.open(base + "/").read().decode()).group(1)


def status(slug="uniservice"):
    return json.loads(call("GET", f"/api/c/{slug}/status")[1])


def line(s):
    parts = [f"{k}:{r['name']}={r['state']}{'!' + r['error'] if r['error'] else ''}" for k in ("rds", "ec2") for r in s[k]]
    o = s["operation"]
    return " | ".join(parts) + (f" | op={o['type']}/{o['status']} step {o['step']}/{len(o['steps'])} '{o['message']}'" if o else "")


if cmd == "status":
    print(line(status()))
elif cmd == "post":
    code, body = call("POST", rest[0])
    print(code, body.strip())
elif cmd == "wait":
    slug, want = rest[0], rest[1]
    deadline = time.time() + float(rest[2] if len(rest) > 2 else 900)
    last = ""
    while time.time() < deadline:
        s = status(slug)
        cur = line(s)
        if cur != last:
            print(time.strftime("%H:%M:%S"), cur, flush=True)
            last = cur
        kinds = os.environ.get("KINDS", "rds,ec2").split(",")
        everything = [r["state"] for k in kinds for r in s[k]]
        o = s["operation"]
        ok_states = {"running", "available"} if want in ("running", "available") else {want}   # RDS says "available"
        if all(x in ok_states for x in everything) and not (o and o["status"] == "running"):
            print("REACHED", want); sys.exit(0)
        if o and o["status"] == "failed":
            print("OPERATION FAILED"); sys.exit(2)
        time.sleep(5)
    print("TIMEOUT"); sys.exit(1)
