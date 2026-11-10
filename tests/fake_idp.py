"""A small but real OpenID Connect provider for tests (discovery, JWKS, authorize, token).

It signs genuine RS256 ID tokens, enforces PKCE and single-use authorization codes, and can be told to misbehave
(tampered state, wrong nonce/audience/issuer, expired token, forged signature, error responses) so the app's
Google sign-in can be tested end to end against real signature and claim validation. Test code only.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time
from urllib.parse import urlencode

from authlib.jose import JsonWebKey, jwt
from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

GOOD_USER = {"sub": "1001", "email": "ada@life180labs.com", "email_verified": True, "hd": "life180labs.com", "name": "Ada Lovelace"}


def _b64url_sha256(text: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()


class FakeIdP:
    def __init__(self, client_id="test-client-id", client_secret="test-client-secret"):
        self.client_id, self.client_secret = client_id, client_secret
        self.key = JsonWebKey.generate_key("RSA", 2048, options={"kid": "test-key-1"}, is_private=True)
        self.attacker_key = JsonWebKey.generate_key("RSA", 2048, options={"kid": "test-key-1"}, is_private=True)  # same kid!

        self.user = dict(GOOD_USER)         # claims about the person "signing in"
        self.claim_overrides: dict = {}     # applied to the next ID token; a value of None deletes the claim
        self.forge_signature = False        # sign with a key that is NOT in the published JWKS
        self.tamper_state = False           # return a different `state` than the one we were given
        self.authorize_error: str | None = None   # answer /authorize with ?error=...

        self.authorize_requests: list[dict] = []
        self.token_requests: list[dict] = []
        self._codes: dict[str, dict] = {}
        self._n = 0

        self.app = Flask("fake-idp")
        self._routes()
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def discovery_url(self) -> str:
        return self.base + "/.well-known/openid-configuration"

    def close(self):
        self._server.shutdown()

    # ------------------------------------------------------------------ endpoints

    def _routes(self):
        app = self.app

        @app.get("/.well-known/openid-configuration")
        def discovery():
            return jsonify(
                issuer=self.base, authorization_endpoint=self.base + "/authorize", token_endpoint=self.base + "/token",
                jwks_uri=self.base + "/jwks", userinfo_endpoint=self.base + "/userinfo",
                response_types_supported=["code"], subject_types_supported=["public"],
                id_token_signing_alg_values_supported=["RS256"], code_challenge_methods_supported=["S256"],
                token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"],
                scopes_supported=["openid", "email", "profile"])

        @app.get("/jwks")
        def jwks():
            return jsonify(keys=[self.key.as_dict(is_private=False)])

        @app.get("/authorize")
        def authorize():
            q = request.args.to_dict()
            self.authorize_requests.append(q)
            if q.get("client_id") != self.client_id or q.get("response_type") != "code":
                return "bad authorize request", 400
            if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
                return "PKCE is required", 400
            back = {"state": "attacker-chosen-state" if self.tamper_state else q.get("state", "")}
            if self.authorize_error:
                back["error"] = self.authorize_error
            else:
                self._n += 1
                code = f"code-{self._n}-{int(time.time() * 1000)}"
                self._codes[code] = q
                back["code"] = code
            return redirect(q["redirect_uri"] + ("&" if "?" in q["redirect_uri"] else "?") + urlencode(back))

        @app.post("/token")
        def token():
            f = request.form.to_dict()
            self.token_requests.append(f)
            auth = request.authorization
            cid = f.get("client_id") or (auth.username if auth else None)
            secret = f.get("client_secret") or (auth.password if auth else None)
            if cid != self.client_id or secret != self.client_secret:
                return jsonify(error="invalid_client"), 401
            asked = self._codes.pop(f.get("code", ""), None)         # pop: a code works exactly once
            if asked is None:
                return jsonify(error="invalid_grant", error_description="unknown or already used code"), 400
            if f.get("redirect_uri") != asked["redirect_uri"]:
                return jsonify(error="invalid_grant", error_description="redirect_uri mismatch"), 400
            if _b64url_sha256(f.get("code_verifier", "")) != asked["code_challenge"]:
                return jsonify(error="invalid_grant", error_description="PKCE verification failed"), 400
            return jsonify(access_token="at-" + f["code"], token_type="Bearer", expires_in=3600,
                           scope=asked.get("scope", ""), id_token=self._id_token(asked))

        @app.get("/userinfo")
        def userinfo():
            return jsonify(self.user)

    # ------------------------------------------------------------------ tokens

    def _id_token(self, asked: dict) -> str:
        now = int(time.time())
        claims = {"iss": self.base, "aud": self.client_id, "iat": now, "exp": now + 300, "nonce": asked.get("nonce"), **self.user}
        for k, v in self.claim_overrides.items():
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        key = self.attacker_key if self.forge_signature else self.key
        return jwt.encode({"alg": "RS256", "kid": "test-key-1"}, claims, key).decode()
