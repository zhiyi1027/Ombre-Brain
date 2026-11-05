"""SIWC contract and credential boundaries using generated keys and fake HTTP."""

import asyncio
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from chatgpt_auth import AUTHORIZE, DIRECT, DISCOVERY, ISSUER, RESOURCE, TOKEN, AuthManager, AuthStore
from chatgpt_login import LoopbackServer, login_handler, main
from clerk_chatgpt import ChatGPTSubscriptionAdapter, configured_adapter, request_body
from clerk_pipeline import ClerkError, EXTRACT_INSTRUCTIONS, NightClerkService, split_sources, encoded_size
from raw_archive import RawArchive

if __package__:
    from .test_clerk_pipeline import DAY, NOW, raw
else:
    from test_clerk_pipeline import DAY, NOW, raw

PRIVATE = "SYNTHETIC_NATIVE_NOT_SENT"
SECRET = "SYNTHETIC_SECRET_TOKEN_517a"
CLIENT = "oaiapp_synthetic"


def fixture_payload():
    return {
        "operation": "extract",
        "instructions": EXTRACT_INSTRUCTIONS,
        "memory_day": str(DAY),
        "missing_context": False,
        "sources": [
            {
                "fragment_id": "f",
                "source_id": "s",
                "source_sha256": "0" * 64,
                "at": "2000-01-01T00:00:00+00:00",
                "speaker": "知知",
                "start": 0,
                "end": 5,
                "text": "虚构午饭。",
            }
        ],
    }


def event(kind, response=None, **extra):
    return (
        "data: "
        + json.dumps({"type": kind, **({"response": response} if response is not None else {}), **extra})
        + "\n\n"
    ).encode()


def completed(reply, usage=None, output=None):
    return event(
        "response.completed",
        {
            "status": "completed",
            "output": output
            if output is not None
            else [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(reply)}],
                }
            ],
            "usage": usage
            or {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "input_tokens_details": {"cached_tokens": 2},
            },
        },
    )


class SubscriptionScenarios(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key())) | {
            "kid": "fixture",
            "alg": "RS256",
            "use": "sig",
        }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="subscription-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthStore(self.root / "auth", create=True)
        self.requests = []
        self.next_tokens = None
        self.token_status = 200
        self.calls = 0

        def serve(request):
            self.requests.append(request)
            if str(request.url) == DISCOVERY:
                return httpx.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "jwks_uri": ISSUER + "/.well-known/jwks.json",
                        "revocation_endpoint": ISSUER + "/revoke",
                    },
                )
            if str(request.url).endswith("/jwks.json"):
                return httpx.Response(200, json={"keys": [self.jwk]})
            if str(request.url) == TOKEN:
                self.calls += 1
                return httpx.Response(self.token_status, json=self.next_tokens or {})
            if str(request.url) == RESOURCE + "/models":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"slug": "fixture-model", "visibility": "list", "display_name": "Fixture"},
                            {"slug": "hidden", "visibility": "hide"},
                        ]
                    },
                )
            if str(request.url) == ISSUER + "/revoke":
                return httpx.Response(200)
            raise AssertionError("unexpected endpoint")

        self.client = httpx.Client(transport=httpx.MockTransport(serve))
        self.addCleanup(self.client.close)
        self.manager = AuthManager(self.store, client=self.client)

    def identity(self, expected_nonce, **extra):
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": CLIENT,
                "sub": "subject-a",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "nonce": expected_nonce,
                "email": "fictional@example.invalid",
                **extra,
            },
            self.key,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )

    def tokens(self, nonce, **extra):
        return {
            "id_token": self.identity(nonce),
            "access_token": SECRET,
            "refresh_token": "synthetic-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email " + DIRECT,
            **extra,
        }

    def connected(self):
        attempt, url = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        self.next_tokens = self.tokens(attempt.nonce)
        self.manager.complete(
            attempt,
            "/auth/callback?" + urlencode({"state": attempt.state, "code": "synthetic-code", "client_id": CLIENT}),
        )
        self.manager.choose_model("main", "fixture-model")
        return self.store.get("main")

    def adapter(self, body):
        self.connected()
        self.posts = []

        def response(request):
            self.posts.append(request)
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

        return ChatGPTSubscriptionAdapter(self.manager, "main", transport=httpx.MockTransport(response))

    def test_private_store_and_disabled_factory_do_not_touch_other_roots(self):
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        with patch("clerk_chatgpt.AuthStore", side_effect=AssertionError("must not access credentials")):
            self.assertIsNone(
                configured_adapter({"night_clerk": {"enabled": False, "provider": "chatgpt_subscription"}})
            )
            self.assertIsNone(configured_adapter({"night_clerk": {"enabled": True}}))
            with self.assertRaises(ClerkError) as blocked:
                configured_adapter(
                    {"night_clerk": {"enabled": True, "provider": "chatgpt_subscription", "auth_dir": "not-read"}}
                )
            self.assertEqual(blocked.exception.code, "subscription_reviewed_input_profile_required")
        for path in (self.root / "vault" / "auth", self.root / ".codex" / "auth"):
            with self.assertRaises(ClerkError):
                AuthStore(path, create=True, vault=self.root / "vault")
        linked = self.root / "linked"
        linked.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaises(ClerkError):
            AuthStore(linked)

    def test_pkce_host_nonce_state_and_returning_registration(self):
        a, url = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        q = parse_qs(urlsplit(url).query)
        self.assertTrue(url.startswith(AUTHORIZE + "?"))
        self.assertEqual(q["client_id"], ["dynamic_agent_client"])
        self.assertEqual(q["ext_agent_host_id"], [self.store.host_id()])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertNotIn(a.verifier, url)
        self.assertNotIn(a.state, repr(a))
        self.assertNotIn(a.nonce, repr(a))
        p = self.connected()
        b, url = self.manager.begin("main", "http://127.0.0.1:54322/auth/callback")
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["client_id"], [CLIENT])
        self.assertNotIn("agent_name_hint", q)
        self.assertEqual(q["id_token_hint"], [p["id_token"]])

    def test_callback_rejects_state_duplicate_replay_expiry_and_client_switch(self):
        for suffix in (
            "state=wrong&code=x&client_id=" + CLIENT,
            "code=x&state=X&state=X",
            "state=X&error=access_denied",
        ):
            a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
            with self.assertRaises(ClerkError):
                a.callback("/auth/callback?" + suffix.replace("X", a.state))
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        a.created -= 301
        with self.assertRaises(ClerkError):
            a.callback("/auth/callback?state=" + a.state)
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        target = "/auth/callback?" + urlencode({"state": a.state, "code": "x", "client_id": CLIENT})
        a.callback(target)
        with self.assertRaises(ClerkError):
            a.callback(target)
        self.connected()
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        with self.assertRaises(ClerkError):
            a.callback("/auth/callback?" + urlencode({"state": a.state, "code": "x", "client_id": "oaiapp_other"}))

    def test_invalid_grant_retains_registration_without_activating_credentials(self):
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        self.token_status = 400
        with self.assertRaises(ClerkError):
            self.manager.complete(
                a, "/auth/callback?" + urlencode({"state": a.state, "code": "x", "client_id": CLIENT})
            )
        p = self.store.get("main")
        self.assertEqual(p["client_id"], CLIENT)
        self.assertNotIn("access_token", p)
        b, url = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        self.assertEqual(b.client_id, CLIENT)

    def test_identity_only_grant_is_retained_but_cannot_infer(self):
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        self.next_tokens = {"id_token": self.identity(a.nonce), "scope": "openid email"}
        r = self.manager.complete(
            a, "/auth/callback?" + urlencode({"state": a.state, "code": "x", "client_id": CLIENT})
        )
        self.assertTrue(r["connected"])
        self.assertFalse(r["plan_usage_enabled"])
        with self.assertRaisesRegex(ClerkError, "permission_required"):
            self.manager.access("main")

    def test_identity_checks_signature_issuer_audience_expiry_nonce_subject(self):
        for claims in (
            {"iss": "https://attacker.invalid"},
            {"aud": "wrong"},
            {"aud": [CLIENT, "other"], "azp": "other"},
            {"exp": int(time.time()) - 60},
            {"sub": ""},
            {"nonce": "wrong"},
        ):
            token = self.identity("expected", **claims)
            with self.assertRaises(ClerkError):
                self.manager.verify_identity(token, CLIENT, nonce="expected")
        token = jwt.encode({"sub": "subject-a"}, "a" * 32, algorithm="HS256", headers={"kid": "fixture"})
        with self.assertRaises(ClerkError):
            self.manager.verify_identity(token, CLIENT, nonce="expected")
        self.connected()
        a, _ = self.manager.begin("main", "http://127.0.0.1:54321/auth/callback")
        self.next_tokens = self.tokens(a.nonce, id_token=self.identity(a.nonce, sub="subject-b"))
        with self.assertRaisesRegex(ClerkError, "account_changed"):
            self.manager.complete(
                a, "/auth/callback?" + urlencode({"state": a.state, "code": "x", "client_id": CLIENT})
            )
        self.assertEqual(self.store.get("main")["subject"], "subject-a")

    def expire(self, **extra):
        with self.store.transaction() as conn:
            p = self.store.get("main", conn)
            p.update(expires_at=time.time() - 1, **extra)
            self.store.put("main", p, conn)

    async def test_refresh_is_serialized_and_rotating_tokens_persist_together(self):
        p = self.connected()
        self.expire()
        self.next_tokens = self.tokens("unused", access_token="new-access", refresh_token="new-refresh")
        self.next_tokens.pop("id_token")
        self.next_tokens.pop("scope")
        before = self.calls
        values = await asyncio.gather(
            asyncio.to_thread(self.manager.access, "main"), asyncio.to_thread(self.manager.access, "main")
        )
        self.assertEqual(values, ["new-access"] * 2)
        self.assertEqual(self.calls, before + 1)
        p = self.store.get("main")
        self.assertEqual(p["refresh_token"], "new-refresh")
        self.assertIn(DIRECT, p["scopes"])

    def test_refresh_reduced_scopes_commits_rotated_token_before_denial(self):
        self.connected()
        self.expire()
        self.next_tokens = self.tokens("unused", scope="openid", refresh_token="new-refresh")
        self.next_tokens.pop("id_token")
        with self.assertRaisesRegex(ClerkError, "permission_required"):
            self.manager.access("main")
        p = self.store.get("main")
        self.assertEqual(p["refresh_token"], "new-refresh")
        self.assertNotIn(DIRECT, p["scopes"])

    def test_refresh_error_pauses_and_earliest_refresh_is_respected(self):
        self.connected()
        self.expire(earliest_refresh_at=time.time() + 60)
        before = self.calls
        with self.assertRaisesRegex(ClerkError, "refresh_not_due"):
            self.manager.access("main")
        self.assertEqual(before, self.calls)
        self.expire(earliest_refresh_at=0)
        self.token_status = 400
        self.next_tokens = {"error": "invalid_grant"}
        with self.assertRaisesRegex(ClerkError, "login_required"):
            self.manager.access("main")
        before = self.calls
        with self.assertRaises(ClerkError):
            self.manager.access("main")
        self.assertEqual(before, self.calls)
        self.assertNotIn("refresh_token", self.store.get("main"))
        self.assertEqual(self.store.get("main")["client_id"], CLIENT)

    def test_models_are_account_specific_and_no_unknown_model_fallback(self):
        self.connected()
        self.assertEqual(self.manager.models("main"), [{"slug": "fixture-model", "display_name": "Fixture"}])
        with self.assertRaises(ClerkError):
            self.manager.choose_model("main", "not-listed")
        self.assertEqual(self.store.get("main")["model"], "fixture-model")

    async def test_only_final_output_text_and_provider_usage_are_returned(self):
        body = event("response.output_text.delta", delta=PRIVATE) + completed(
            {"facts": []},
            output=[
                {"type": "reasoning", "summary": [{"text": PRIVATE}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"facts":[]}'}]},
            ],
        )
        adapter = self.adapter(body)
        result = await adapter.generate(fixture_payload())
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["usage"]["cached_input_tokens"], 2)
        self.assertNotIn(PRIVATE, json.dumps(result))
        request = self.posts[0]
        wire = json.loads(request.content)
        self.assertEqual(str(request.url), RESOURCE + "/responses")
        self.assertEqual(set(wire), {"model", "instructions", "input", "store", "stream"})
        self.assertFalse(wire["store"])
        self.assertTrue(wire["stream"])
        self.assertEqual(request.headers["authorization"], "Bearer " + SECRET)

    async def test_partial_done_incomplete_and_tool_results_are_never_success(self):
        for body in (
            event("response.output_text.delta", delta='{"facts":[]}'),
            b"data: [DONE]\n\n",
            event("response.incomplete", {"status": "incomplete"}),
            completed({}, output=[{"type": "function_call", "arguments": "{}"}]),
        ):
            adapter = self.adapter(body)
            with self.assertRaises(ClerkError):
                await adapter.generate(fixture_payload())

    async def test_midstream_quota_failure_pauses_next_request_without_paid_fallback(self):
        for code in ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible"):
            for failure in (
                event("response.failed", {"error": {"code": code}}),
                event("error", code=code, message=PRIVATE),
            ):
                adapter = self.adapter(event("response.output_text.delta", delta="partial") + failure)
                with self.assertRaisesRegex(ClerkError, code):
                    await adapter.generate(fixture_payload())
                with self.assertRaises(ClerkError):
                    await adapter.generate(fixture_payload())
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(self.store.get("main")["paused"], code)

    async def test_unstructured_429_and_redirect_do_not_retry_or_follow(self):
        self.connected()
        hits = []

        def response(request):
            hits.append(request)
            return httpx.Response(429, json={"detail": PRIVATE})

        adapter = ChatGPTSubscriptionAdapter(self.manager, "main", transport=httpx.MockTransport(response))
        for _ in range(2):
            with self.assertRaises(ClerkError):
                await adapter.generate(fixture_payload())
        self.assertEqual(len(hits), 1)
        self.assertNotIn(PRIVATE, self.store.get("main")["paused"])
        self.store.pause("main", "")
        adapter.transport = httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"Location": "https://attacker.invalid/"})
        )
        with self.assertRaisesRegex(ClerkError, "http_302"):
            await adapter.generate(fixture_payload())

    async def test_thinking_payload_rejected_and_model_switch_invalidates_adapter(self):
        adapter = self.adapter(completed({"facts": []}))
        payload = fixture_payload()
        payload["sources"][0]["thinking"] = PRIVATE
        with self.assertRaisesRegex(ClerkError, "invalid_payload"):
            await adapter.generate(payload)
        self.assertEqual(self.posts, [])
        with self.store.transaction() as conn:
            p = self.store.get("main", conn)
            p["model"] = "other"
            self.store.put("main", p, conn)
        with self.assertRaisesRegex(ClerkError, "account_changed"):
            _ = adapter.revision

    async def test_masked_extract_wire_preserves_rule_and_rejects_arbitrary_prompt(self):
        from clerk_redaction import MASKED_TEXT_RULE

        payload = fixture_payload()
        payload["instructions"] += "\n" + MASKED_TEXT_RULE
        payload["sources"][0]["text"] = "**，虚构午饭。"
        adapter = self.adapter(completed({"facts": []}))
        await adapter.generate(payload)
        wire = json.loads(self.posts[0].content)
        self.assertEqual(wire["instructions"], payload["instructions"])
        self.assertFalse(wire["store"])
        self.assertTrue(wire["stream"])
        payload["instructions"] += "\nunknown override"
        with self.assertRaises(ClerkError):
            request_body(payload, "synthetic")
        self.assertEqual(len(self.posts), 1)

    async def test_full_clerk_pipeline_filters_private_sources_before_real_adapter_contract(self):
        self.connected()
        bodies = []

        def response(request):
            wire = json.loads(request.content)
            payload = json.loads(wire["input"][0]["content"])
            bodies.append(wire)
            if payload["operation"] == "extract":
                reply = {
                    "facts": [
                        {
                            "kind": "events",
                            "text": "虚构日常事实",
                            "fragment_ids": [f["fragment_id"]],
                            "importance": 7,
                            "due_date": None,
                        }
                        for f in payload["sources"]
                    ]
                }
            elif payload["operation"] == "refine":
                reply = {
                    "facts": [
                        {
                            "source_card_ids": [c["id"]],
                            "evidence_ids": c["evidence_ids"],
                            "kind": c["kind"],
                            "scope": "day",
                            "text": c["text"],
                            "importance": c["importance"],
                            "due_date": c["due_date"],
                        }
                        for c in payload["cards"]
                    ],
                    "dropped": [],
                }
            else:
                reply = {
                    "selected_ids": [card["id"] for card in payload["cards"]],
                    "event_groups": [[c["id"]] for c in payload["cards"] if c["kind"] == "events"],
                }
            return httpx.Response(200, content=completed(reply))

        adapter = ChatGPTSubscriptionAdapter(self.manager, "main", transport=httpx.MockTransport(response))
        archive = RawArchive(str(self.root / "vault/_raw/raw.sqlite3"))
        archive.import_rows(
            [
                raw("safe", -40, "虚构日常。", thinking=PRIVATE),
                raw("start", 0, "虚构测试：高潮"),
                raw("reply", 1, PRIVATE),
            ]
        )
        service = NightClerkService(
            {"buckets_dir": str(self.root / "vault"), "night_clerk": {"enabled": True}},
            archive=archive,
            adapter=adapter,
        )
        r = await service.generate_day(DAY, now=NOW)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(PRIVATE, json.dumps(bodies, ensure_ascii=False))
        self.assertNotIn(SECRET, json.dumps(service.store.publication(str(DAY))))

    def test_signout_retains_registration_clears_tokens_and_reports_revocation(self):
        self.connected()
        r = self.manager.sign_out("main")
        self.assertTrue(r["remote_revocation_confirmed"])
        p = self.store.get("main")
        self.assertEqual(p["client_id"], CLIENT)
        self.assertNotIn("refresh_token", p)
        with self.assertRaises(ClerkError):
            self.manager.access("main")

    def test_loopback_handler_hides_callback_errors_and_rejects_host_and_csrf(self):
        result = {}
        handler = login_handler(self.manager, "main", result)
        # Exercise handlers in memory. No port, browser, or real account is used.
        h = object.__new__(handler)
        h.server = type(
            "Server", (), {"server_port": 54321, "attempt": None, "ticket": "synthetic-ticket", "finished": False}
        )()
        h.path = "/start"
        h.headers = {"Host": "evil.invalid", "Content-Length": "0"}
        h.rfile = io.BytesIO()
        answers = []
        h.answer = lambda status, body: answers.append((status, body))
        h.do_POST()
        self.assertEqual(answers[-1][0], 403)
        h.headers = {"Host": "127.0.0.1:54321", "Content-Length": "12"}
        h.rfile = io.BytesIO(b"ticket=wrong")
        h.do_POST()
        self.assertEqual(answers[-1][0], 403)
        h.path = "/"
        h.do_GET()
        self.assertIn("Continue with ChatGPT", answers[-1][1])

    def test_cli_rejects_vault_auth_path_before_creating_anything(self):
        root = self.root / "vault"
        with patch("sys.stdout", new=io.StringIO()):
            code = main(["--auth-dir", str(root / "auth"), "--vault", str(root), "status"])
        self.assertEqual(code, 1)
        self.assertFalse(root.exists())

    def test_unusable_refresh_codes_clear_tokens_but_temporary_and_config_errors_retain(self):
        for error in (
            "invalid_grant",
            "invalid_refresh_token",
            "refresh_token_expired",
            "refresh_token_reused",
            "refresh_token_invalidated",
            "token_expired",
            "invalid_client",
            None,
        ):
            with self.subTest(error=error):
                self.token_status = 200
                self.connected()
                self.expire()
                self.token_status = 500 if error is None else 400
                self.next_tokens = {"error": error}
                with self.assertRaises(ClerkError):
                    self.manager.access("main")
                p = self.store.get("main")
                self.assertEqual(p["client_id"], CLIENT)
                if error in ("invalid_client", None):
                    self.assertIn("refresh_token", p)
                else:
                    self.assertNotIn("refresh_token", p)

    def test_http_ineligible_keeps_exact_pause_code(self):
        self.connected()
        adapter = ChatGPTSubscriptionAdapter(self.manager, "main")
        code = "subscription_sharing_user_not_eligible"
        with self.assertRaisesRegex(ClerkError, code):
            adapter.fail(code, 403)
        self.assertEqual(self.store.get("main")["paused"], code)

    def test_wire_budget_includes_transport_json_and_usage_upper_bound(self):
        self.connected()
        adapter = ChatGPTSubscriptionAdapter(self.manager, "main")
        archive = RawArchive(str(self.root / "vault/_raw/raw.sqlite3"))
        archive.import_rows([raw("long", 0, '虚构\\引号"。' * 1800)])
        service = NightClerkService(
            {"buckets_dir": str(self.root / "vault"), "night_clerk": {"enabled": True}},
            archive=archive,
            adapter=adapter,
        )
        day = service.prepare(DAY, NOW)
        chunks = split_sources(day, 8000, adapter.request_size)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(encoded_size(request_body(c, adapter.model)) <= 8000 for c in chunks))
        joined = "".join(f["text"] for c in chunks for f in c["sources"])
        self.assertEqual(joined, day.messages[0].text)

    def test_accepted_socket_has_absolute_deadline_even_if_input_trickles(self):
        from http.server import HTTPServer
        from unittest.mock import MagicMock

        server = object.__new__(LoopbackServer)
        server.deadline = time.monotonic() + 10
        server.deadline_timers = {}
        sock = MagicMock()
        timer = MagicMock()
        callbacks = []

        def capture(seconds, callback):
            callbacks.append((seconds, callback))
            return timer

        with (
            patch.object(HTTPServer, "get_request", return_value=(sock, ("127.0.0.1", 1))),
            patch("chatgpt_login.threading.Timer", side_effect=capture),
        ):
            server.get_request()
        self.assertLessEqual(callbacks[0][0], 10)
        timer.start.assert_called_once()
        callbacks[0][1]()
        sock.shutdown.assert_called_once()
        sock.close.assert_called_once()
        server.close_request(sock)
        timer.cancel.assert_called_once()

    def test_hardlinked_credential_file_is_rejected_before_writing_secrets(self):
        other = self.root / "other-auth"
        other.mkdir(mode=0o700)
        (other / "ob-chatgpt.sqlite3").hardlink_to(self.store.path)
        with self.assertRaisesRegex(ClerkError, "file_not_private"):
            AuthStore(other)

    def test_login_form_preserves_origin_without_weakening_csrf_or_callback_privacy(self):
        from chatgpt_login import _PAGE

        self.assertIn('name="referrer" content="same-origin"', _PAGE)
        handler = login_handler(self.manager, "main", {})
        h = object.__new__(handler)
        h.server = type(
            "Server", (), {"server_port": 54321, "attempt": None, "ticket": "synthetic-ticket", "finished": False}
        )()
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        h.wfile = io.BytesIO()
        h.path = "/"
        h.answer(200, "entry")
        h.send_header.assert_any_call("Referrer-Policy", "same-origin")
        h.send_header.reset_mock()
        h.path = "/auth/callback?code=synthetic"
        h.answer(200, "callback")
        h.send_header.assert_any_call("Referrer-Policy", "no-referrer")
        for origin in ("null", "http://attacker.invalid", "http://127.0.0.1:54321"):
            with self.subTest(origin=origin):
                h.server.attempt = None
                h.path = "/start"
                body = b"ticket=synthetic-ticket"
                h.headers = {"Host": "127.0.0.1:54321", "Origin": origin, "Content-Length": str(len(body))}
                h.rfile = io.BytesIO(body)
                h.send_response.reset_mock()
                h.do_POST()
                h.send_response.assert_called_with(303 if origin == "http://127.0.0.1:54321" else 403)

    async def test_non_json_http_error_preserves_status_and_pauses_where_required(self):
        self.connected()
        posts = []

        def response(request):
            posts.append(request)
            return httpx.Response(503, content=b"<html>upstream unavailable</html>")

        adapter = ChatGPTSubscriptionAdapter(self.manager, "main", transport=httpx.MockTransport(response))
        for _ in range(2):
            with self.assertRaisesRegex(ClerkError, "subscription_http_503"):
                await adapter.generate(fixture_payload())
        self.assertEqual(len(posts), 1)

    async def test_done_items_wait_for_completed_and_supply_omitted_terminal_snapshot(self):
        item = {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": '{"facts":[]}'}],
        }
        done = event("response.output_item.done", output_index=0, item=item)
        for terminal in (completed({}, output=[]), event("response.completed", {"status": "completed", "usage": {}})):
            adapter = self.adapter(done + terminal)
            result = await adapter.generate(fixture_payload())
            self.assertEqual(result["facts"], [])
        for tail in (
            b"",
            b"data: [DONE]\n\n",
            event("response.incomplete", {"status": "incomplete"}),
            event("response.failed", {"error": {}}),
        ):
            adapter = self.adapter(done + tail)
            with self.assertRaises(ClerkError):
                await adapter.generate(fixture_payload())

    async def test_done_item_conflicts_response_ids_and_partial_items_are_rejected(self):
        item = {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": '{"facts":[]}'}],
        }
        done = event("response.output_item.done", output_index=0, item=item)
        conflicts = [
            done + completed({}, output=[item | {"id": "different"}]),
            done
            + event("response.output_item.done", output_index=0, item=item | {"id": "different"})
            + completed({}, output=[]),
            event("response.created", {"id": "resp_one"})
            + done
            + event("response.completed", {"id": "resp_two", "status": "completed", "output": []}),
            event("response.output_item.done", output_index=0, item=item | {"status": "in_progress"})
            + completed({}, output=[]),
        ]
        for body in conflicts:
            adapter = self.adapter(body)
            with self.assertRaises(ClerkError):
                await adapter.generate(fixture_payload())

    async def test_streamed_reasoning_is_ignored_and_streamed_tools_rejected(self):
        reason = event(
            "response.output_item.done",
            output_index=0,
            item={"id": "r", "type": "reasoning", "summary": [{"text": PRIVATE}]},
        )
        msg = event(
            "response.output_item.done",
            output_index=1,
            item={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"facts":[]}'}],
            },
        )
        adapter = self.adapter(reason + msg + completed({}, output=[]))
        result = await adapter.generate(fixture_payload())
        self.assertNotIn(PRIVATE, json.dumps(result))
        tool = event(
            "response.output_item.done", output_index=0, item={"id": "t", "type": "function_call", "arguments": "{}"}
        )
        adapter = self.adapter(tool + completed({}, output=[]))
        with self.assertRaises(ClerkError):
            await adapter.generate(fixture_payload())

    async def test_announced_messages_and_explicit_response_scope_must_all_complete(self):
        item = {
            "id": "m",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": '{"facts":[]}'}],
        }
        added = event(
            "response.output_item.added", output_index=0, item=item | {"status": "in_progress", "content": []}
        )
        done = event("response.output_item.done", output_index=1, item=item | {"id": "other"})
        adapter = self.adapter(added + done + completed({}, output=[]))
        with self.assertRaises(ClerkError):
            await adapter.generate(fixture_payload())
        body = (
            event("response.created", {"id": "one"})
            + event("response.output_item.done", response_id="two", output_index=0, item=item)
            + completed({}, output=[])
        )
        adapter = self.adapter(body)
        with self.assertRaises(ClerkError):
            await adapter.generate(fixture_payload())


if __name__ == "__main__":
    unittest.main()
