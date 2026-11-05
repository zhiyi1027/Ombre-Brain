"""Independent local SIWC credentials. No Codex/Claude credential reuse.

Credentials live outside vault/source control. Network endpoints are fixed;
HTTP clients disable redirects and environment credentials/proxies. Callers
must never log this module's private records or authorization URLs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import httpx
import jwt

from clerk_pipeline import ClerkError

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DIRECT = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT
USAGE_URL = "https://chatgpt.com/#settings/Usage"
_LABEL = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_CLIENT = re.compile(r"^oaiapp_[A-Za-z0-9_-]{1,200}$")


def require(condition, code="subscription_invalid_credentials"):
    if not condition:
        raise ClerkError(code)


def fingerprint(profile):
    return hashlib.sha256(json.dumps([profile.get(k) for k in ("issuer", "subject", "client_id")],
                                     separators=(",", ":")).encode()).hexdigest()


class AuthStore:
    """One private SQLite file, serialized refresh across CLI/server processes."""
    def __init__(self, root, *, create=False, vault=None):
        original = Path(root).expanduser()
        require(original.is_absolute(), "subscription_auth_path_invalid")
        for part in (original, *original.parents):
            require(not part.is_symlink(), "subscription_auth_path_invalid")
            require(part.name not in (".codex", ".claude", ".aws") and not (part / ".git").exists(),
                    "subscription_auth_path_invalid")
        self.root = original.resolve()
        if vault:
            require(not self.root.is_relative_to(Path(vault).resolve()), "subscription_auth_path_invalid")
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(self.root.is_dir() and stat.S_IMODE(self.root.stat().st_mode) == 0o700,
                "subscription_auth_directory_not_private")
        self.path = self.root / "ob-chatgpt.sqlite3"
        if create:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        require(self.path.is_file() and not self.path.is_symlink(), "subscription_auth_file_not_private")
        metadata = self.path.stat()
        require(stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
                and metadata.st_uid == os.getuid(), "subscription_auth_file_not_private")
        with self.transaction() as conn:
            conn.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                               "CREATE TABLE IF NOT EXISTS profiles(label TEXT PRIMARY KEY,data TEXT NOT NULL);")
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('host_id',?)", ("urn:uuid:" + str(uuid.uuid4()),))

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, label, conn=None):
        require(isinstance(label, str) and _LABEL.fullmatch(label), "subscription_profile_invalid")
        if conn is None:
            with self.transaction() as opened:
                return self.get(label, opened)
        row = conn.execute("SELECT data FROM profiles WHERE label=?", (label,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self, label, profile, conn):
        require(_LABEL.fullmatch(label), "subscription_profile_invalid")
        conn.execute("INSERT OR REPLACE INTO profiles VALUES (?,?)", (label, json.dumps(profile)))

    def host_id(self):
        with self.transaction() as conn:
            return conn.execute("SELECT value FROM meta WHERE key='host_id'").fetchone()[0]

    def summaries(self):
        with self.transaction() as conn:
            rows = conn.execute("SELECT label,data FROM profiles ORDER BY label").fetchall()
        return [{"profile": label, "email": p.get("email", ""), "connected": bool(p.get("subject") and p.get("id_token")),
                 "plan_usage_enabled": DIRECT in p.get("scopes", []), "model": p.get("model", ""),
                 "paused": p.get("paused", "")} for label, body in rows for p in (json.loads(body),)]

    def pause(self, label, code):
        with self.transaction() as conn:
            p = self.get(label, conn)
            p["paused"] = code
            self.put(label, p, conn)


@dataclass(repr=False)
class LoginAttempt:
    label: str
    redirect_uri: str
    client_id: str
    expected_subject: str | None
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    created: float = field(default_factory=time.monotonic)
    consumed: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)

    def __repr__(self):
        return "<LoginAttempt private>"

    def callback(self, target):
        with self.lock:
            return self._callback(target)

    def _callback(self, target):
        require(not self.consumed and time.monotonic() - self.created <= 300,
                "subscription_login_expired")
        parsed = urlsplit(target)
        query = parse_qs(parsed.query, keep_blank_values=True)
        require(parsed.path == "/auth/callback" and not parsed.fragment
                and all(len(values) == 1 for values in query.values()), "subscription_callback_invalid")
        require(hmac.compare_digest(query.get("state", [""])[0], self.state), "subscription_state_mismatch")
        self.consumed = True
        require("error" not in query, "subscription_consent_declined")
        issued = query.get("client_id", [self.client_id])[0]
        require(_CLIENT.fullmatch(issued), "subscription_registration_missing")
        require(self.client_id == "dynamic_agent_client" or issued == self.client_id,
                "subscription_account_changed")
        code = query.get("code", [""])[0]
        require(bool(code) and len(code) <= 8192, "subscription_callback_invalid")
        return issued, code


class AuthManager:
    def __init__(self, store, *, client=None):
        self.store = store
        self.client = client

    @contextmanager
    def http(self):
        if self.client is not None:
            yield self.client
        else:
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                yield client

    def json_request(self, method, url, **kwargs):
        with self.http() as client:
            response = client.request(method, url, **kwargs)
        require(response.status_code == 200, "subscription_auth_http_" + str(response.status_code))
        require(len(response.content) <= 1024 * 1024, "subscription_auth_response_invalid")
        try:
            value = response.json()
            require(isinstance(value, dict), "subscription_auth_response_invalid")
            return value
        except (TypeError, ValueError):
            raise ClerkError("subscription_auth_response_invalid") from None

    def verify_identity(self, token, client_id, *, nonce=None):
        require(isinstance(token, str) and len(token) <= 32768)
        discovery = self.json_request("GET", DISCOVERY)
        require(discovery.get("issuer") == ISSUER, "subscription_identity_invalid")
        jwks_uri = discovery.get("jwks_uri", "")
        parsed = urlsplit(jwks_uri)
        require(parsed.scheme == "https" and parsed.netloc == "auth.openai.com"
                and not parsed.query and not parsed.fragment, "subscription_identity_invalid")
        jwks = self.json_request("GET", jwks_uri)
        try:
            header = jwt.get_unverified_header(token)
            require(header.get("alg") in ("RS256", "ES256") and isinstance(header.get("kid"), str),
                    "subscription_identity_invalid")
            keys = jwks.get("keys")
            require(isinstance(keys, list) and len(keys) <= 32, "subscription_identity_invalid")
            matches = [key for key in keys if key.get("kid") == header["kid"]]
            require(len(matches) == 1, "subscription_identity_invalid")
            key = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"])
            claims = jwt.decode(token, key.key, algorithms=[header["alg"]], issuer=ISSUER,
                                audience=client_id, leeway=5,
                                options={"require": ["iss", "aud", "sub", "exp", "iat"]})
            if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1:
                require(claims.get("azp") == client_id, "subscription_identity_invalid")
            require(isinstance(claims.get("sub"), str) and bool(claims["sub"]), "subscription_identity_invalid")
            if nonce is not None:
                require(isinstance(claims.get("nonce"), str) and hmac.compare_digest(claims["nonce"], nonce),
                        "subscription_nonce_mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise ClerkError("subscription_identity_invalid") from None

    def begin(self, label, redirect_uri):
        parsed = urlsplit(redirect_uri)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                and parsed.path == "/auth/callback" and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password, "subscription_callback_invalid")
        p = self.store.get(label)
        attempt = LoginAttempt(label, redirect_uri, p.get("client_id") or "dynamic_agent_client", p.get("subject"))
        query = {"client_id": attempt.client_id, "ext_agent_host_id": self.store.host_id(),
                 "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES, "resource": RESOURCE,
                 "state": attempt.state, "nonce": attempt.nonce, "code_challenge_method": "S256",
                 "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()}
        if attempt.client_id == "dynamic_agent_client":
            query["agent_name_hint"] = "Ombre Brain"
        if p.get("id_token"):
            query["id_token_hint"] = p["id_token"]
        return attempt, AUTHORIZE + "?" + urlencode(query)

    def _token_record(self, data, old, claims):
        scopes = data.get("scope", " ".join(old.get("scopes", [])))
        require(isinstance(scopes, str))
        granted = scopes.split()
        access, refresh = data.get("access_token", ""), data.get("refresh_token", "")
        require(isinstance(access, str) and not any(c in access for c in "\r\n"))
        require(isinstance(refresh, str))
        if DIRECT in granted:
            require(bool(access) and bool(refresh))
        if access:
            require(str(data.get("token_type", "")).lower() == "bearer")
        expires = data.get("expires_in", 0)
        require(type(expires) is int and 0 <= expires <= 86400 and (not access or expires > 0))
        earliest = data.get("earliest_refresh_at", 0)
        if isinstance(earliest, str):
            try:
                earliest = datetime.fromisoformat(earliest.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                raise ClerkError("subscription_invalid_credentials") from None
        require(type(earliest) in (int, float) and 0 <= earliest < 100000000000)
        return {**old, "issuer": ISSUER, "subject": claims["sub"], "email": claims.get("email", old.get("email", "")),
                "access_token": access, "refresh_token": refresh, "id_token": data.get("id_token") or old.get("id_token"),
                "scopes": granted, "expires_at": time.time() + expires, "earliest_refresh_at": earliest, "paused": ""}

    def complete(self, attempt, target):
        client_id, code = attempt.callback(target)
        # Keep a completed registration even if the one-time code expires.
        with self.store.transaction() as conn:
            old = self.store.get(attempt.label, conn)
            require((old.get("client_id") or "dynamic_agent_client") == attempt.client_id,
                    "subscription_account_changed")
            old["client_id"] = client_id
            self.store.put(attempt.label, old, conn)
        data = self.json_request("POST", TOKEN, data={"grant_type": "authorization_code", "client_id": client_id,
                                "code": code, "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri,
                                "resource": RESOURCE})
        claims = self.verify_identity(data.get("id_token"), client_id, nonce=attempt.nonce)
        require(not attempt.expected_subject or claims["sub"] == attempt.expected_subject,
                "subscription_account_changed")
        with self.store.transaction() as conn:
            current = self.store.get(attempt.label, conn)
            require(current.get("client_id") == client_id, "subscription_account_changed")
            p = self._token_record(data, current, claims)
            self.store.put(attempt.label, p, conn)
        return {"profile": attempt.label, "connected": True, "plan_usage_enabled": DIRECT in p["scopes"]}

    def access(self, label, *, expected=None):
        problem = None
        with self.store.transaction() as conn:
            p = self.store.get(label, conn)
            require(p.get("subject") and p.get("client_id"), "subscription_login_required")
            require(expected is None or fingerprint(p) == expected, "subscription_account_changed")
            require(not p.get("paused"), p.get("paused") or "subscription_paused")
            require(DIRECT in p.get("scopes", []), "subscription_permission_required")
            require(bool(p.get("access_token")), "subscription_login_required")
            if p.get("expires_at", 0) <= time.time() + 60:
                if p.get("earliest_refresh_at", 0) > time.time():
                    require(p["expires_at"] > time.time() + 5, "subscription_refresh_not_due")
                else:
                    try:
                        with self.http() as client:
                            response = client.post(TOKEN, data={"grant_type": "refresh_token", "client_id": p["client_id"],
                                                   "refresh_token": p["refresh_token"], "resource": RESOURCE})
                        if response.status_code != 200:
                            try:
                                error = response.json().get("error") if len(response.content) <= 65536 else None
                                code = error.get("code") if isinstance(error, dict) else error
                            except (TypeError, ValueError, AttributeError):
                                code = None
                            if code in {"invalid_grant", "invalid_refresh_token", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated", "token_expired"}:
                                for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                                    p.pop(key, None)
                                p["paused"] = "subscription_login_required"
                            elif code == "invalid_client":
                                p["paused"] = "subscription_client_configuration_invalid"
                            else:
                                p["paused"] = "subscription_refresh_unavailable"
                            problem = p["paused"]
                        else:
                            require(len(response.content) <= 1024 * 1024)
                            data = response.json()
                            claims = self.verify_identity(data["id_token"], p["client_id"]) if data.get("id_token") else p
                            require(claims.get("sub", claims.get("subject")) == p["subject"], "subscription_account_changed")
                            p = self._token_record(data, p, {"sub": p["subject"], "email": claims.get("email", "")})
                            if DIRECT not in p["scopes"]:
                                problem = "subscription_permission_required"
                    except httpx.HTTPError:
                        p["paused"] = problem = "subscription_refresh_unavailable"
                    except (ClerkError, TypeError, ValueError):
                        p["paused"] = problem = "subscription_login_required"
                    self.store.put(label, p, conn)
            token = p.get("access_token", "")
        # Commit rotated tokens and reduced scopes before reporting a permission failure.
        if problem:
            raise ClerkError(problem)
        return token

    def models(self, label):
        identity = fingerprint(self.store.get(label))
        token = self.access(label, expected=identity)
        data = self.json_request("GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
        require(isinstance(data.get("models"), list), "subscription_models_invalid")
        require(fingerprint(self.store.get(label)) == identity, "subscription_account_changed")
        return [{"slug": m["slug"], "display_name": m.get("display_name") or m["slug"]}
                for m in data["models"] if isinstance(m, dict) and m.get("visibility") == "list"
                and isinstance(m.get("slug"), str)]

    def choose_model(self, label, model):
        identity = fingerprint(self.store.get(label))
        require(model in {m["slug"] for m in self.models(label)}, "subscription_model_unavailable")
        with self.store.transaction() as conn:
            p = self.store.get(label, conn)
            require(fingerprint(p) == identity, "subscription_account_changed")
            p["model"] = model
            self.store.put(label, p, conn)

    def sign_out(self, label):
        confirmed = False
        with self.store.transaction() as conn:
            p = self.store.get(label, conn)
            if p.get("refresh_token"):
                try:
                    discovery = self.json_request("GET", DISCOVERY)
                    url = discovery.get("revocation_endpoint", "")
                    require(urlsplit(url).scheme == "https" and urlsplit(url).netloc == "auth.openai.com")
                    with self.http() as client:
                        response = client.post(url, data={"token": p["refresh_token"], "token_type_hint": "refresh_token",
                                                          "client_id": p["client_id"]})
                    confirmed = response.status_code == 200
                except (ClerkError, httpx.HTTPError):
                    confirmed = False
            for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
                p.pop(key, None)
            p["paused"] = "subscription_login_required"
            self.store.put(label, p, conn)
        return {"signed_out": True, "remote_revocation_confirmed": confirmed, "manage_usage": USAGE_URL}
