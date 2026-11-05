"""Subscription-only Responses adapter; no local tools, API keys or fallback."""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from chatgpt_auth import AuthManager, AuthStore, RESOURCE, fingerprint, require
from clerk_pipeline import (
    ClerkError,
    CAPS,
    KINDS,
    EXTRACT_INSTRUCTIONS,
    SELECT_INSTRUCTIONS,
    SHORT_FRAGMENT_RULE,
    SPEAKER_EVIDENCE_RULE,
    PLAIN_SUMMARY_RULE,
    encoded_size,
)
from utils import parse_bool

_PAUSE_CODES = {
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_user_not_eligible",
    "subscription_sharing_usage_unavailable",
}


def _json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = item
        return result

    try:
        return json.loads(
            value, object_pairs_hook=unique, parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("nonfinite"))
        )
    except (TypeError, ValueError):
        raise ClerkError("subscription_invalid_response") from None


def request_body(payload, model, *, measure_only=False):
    """Whitelist the existing clerk wire contract, never general raw rows."""
    require(isinstance(payload, dict), "subscription_invalid_payload")
    operation = payload.get("operation")
    if operation == "extract":
        from clerk_redaction import MASKED_TEXT_RULE

        masked_instructions = EXTRACT_INSTRUCTIONS + "\n" + MASKED_TEXT_RULE
        allowed_instructions = (
            EXTRACT_INSTRUCTIONS,
            masked_instructions,
            masked_instructions + "\n" + SHORT_FRAGMENT_RULE,
            masked_instructions + "\n" + SHORT_FRAGMENT_RULE + "\n" + SPEAKER_EVIDENCE_RULE,
            masked_instructions + "\n" + SHORT_FRAGMENT_RULE + "\n" + SPEAKER_EVIDENCE_RULE + "\n" + PLAIN_SUMMARY_RULE,
        )
        require(
            set(payload) == {"operation", "instructions", "memory_day", "missing_context", "sources"}
            and payload["instructions"] in allowed_instructions,
            "subscription_invalid_payload",
        )
        require(isinstance(payload["sources"], list), "subscription_invalid_payload")
        for row in payload["sources"]:
            require(
                isinstance(row, dict)
                and set(row) == {"fragment_id", "source_id", "source_sha256", "at", "speaker", "start", "end", "text"}
                and isinstance(row["text"], str),
                "subscription_invalid_payload",
            )
            require(
                all(isinstance(row[k], str) for k in ("fragment_id", "source_id", "source_sha256", "at", "speaker"))
                and row["speaker"] in ("知知", "顾凛")
                and type(row["start"]) is int
                and type(row["end"]) is int
                and 0 <= row["start"] <= row["end"],
                "subscription_invalid_payload",
            )
        require(
            isinstance(payload["memory_day"], str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["memory_day"])
            and type(payload["missing_context"]) is bool,
            "subscription_invalid_payload",
        )
        instructions = payload["instructions"]
    elif operation == "refine":
        from clerk_refine import valid_refinement_payload

        require(valid_refinement_payload(payload), "subscription_invalid_payload")
        instructions = payload["instructions"]
    elif operation in ("mood_scan", "followup_review"):
        from clerk_quality import valid_payload

        require(valid_payload(payload), "subscription_invalid_payload")
        instructions = payload["instructions"]
    elif operation == "select":
        from clerk_quality import VERSION, SELECT_INSTRUCTIONS as QUALITY_SELECT, CLASSES

        quality = payload.get("quality_version") == VERSION
        expected = {"operation", "instructions", "limit", "caps", "cards", "selection_revision", "event_ids"}
        if quality:
            expected.add("quality_version")
        require(
            set(payload) == expected
            and payload["instructions"] == (QUALITY_SELECT if quality else SELECT_INSTRUCTIONS),
            "subscription_invalid_payload",
        )
        require(
            isinstance(payload["selection_revision"], str)
            and re.fullmatch(r"[0-9a-f]{64}", payload["selection_revision"]),
            "subscription_invalid_payload",
        )
        fields = {"id", "kind", "text", "importance"} | (
            {"priority", "impression_class", "verbatim"} if quality else set()
        )
        require(
            isinstance(payload["cards"], list)
            and all(
                isinstance(card, dict) and set(card) == fields and isinstance(card["text"], str)
                for card in payload["cards"]
            ),
            "subscription_invalid_payload",
        )
        require(
            type(payload["limit"]) is int
            and 1 <= payload["limit"] <= sum(CAPS.values())
            and payload["caps"] == CAPS
            and all(
                isinstance(c["id"], str)
                and c["kind"] in KINDS
                and type(c["importance"]) is int
                and 1 <= c["importance"] <= 10
                for c in payload["cards"]
            ),
            "subscription_invalid_payload",
        )
        require(
            payload["event_ids"] == [c["id"] for c in payload["cards"] if c["kind"] == "events"],
            "subscription_invalid_payload",
        )
        if quality:
            require(
                all(
                    type(c["priority"]) is int
                    and 0 <= c["priority"] <= 4
                    and c["impression_class"] in CLASSES
                    and isinstance(c["verbatim"], str)
                    for c in payload["cards"]
                ),
                "subscription_invalid_payload",
            )
        instructions = payload["instructions"]
    else:
        raise ClerkError("subscription_invalid_payload")
    require(type(measure_only) is bool, "subscription_invalid_payload")
    content = {k: v for k, v in payload.items() if k != "instructions"}
    body = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": json.dumps(content, ensure_ascii=False, separators=(",", ":"))}],
        "store": False,
        "stream": True,
    }
    require(measure_only or encoded_size(body) <= 128000, "subscription_invalid_payload")
    return body


async def sse_events(response):
    """Bound raw bytes before line splitting; do not retain/log stream events."""
    pending, data, total = b"", [], 0
    async for block in response.aiter_bytes():
        total += len(block)
        require(total <= 4 * 1024 * 1024, "subscription_stream_too_large")
        pending += block
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if data:
                    joined = b"\n".join(data)
                    data = []
                    if joined == b"[DONE]":
                        return
                    yield _json(joined)
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
    # An unterminated event is not a completed response.


def final_message(item, *, streamed=False):
    require(isinstance(item, dict), "subscription_invalid_response")
    if item.get("type") == "reasoning":
        return None
    require(item.get("type") == "message" and item.get("role") == "assistant", "subscription_unexpected_tool_output")
    require(
        item.get("status") in (("completed",) if streamed else (None, "completed")), "subscription_invalid_response"
    )
    content = item.get("content")
    require(isinstance(content, list) and content, "subscription_invalid_response")
    texts = []
    for part in content:
        require(
            isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str),
            "subscription_invalid_response",
        )
        texts.append(part["text"])
    text = "".join(texts)
    require(len(text.encode()) <= 128000, "subscription_response_too_large")
    return {"id": item.get("id"), "text": text}


class ChatGPTSubscriptionAdapter:
    provider = "openai-chatgpt-plan"

    def __init__(self, auth, profile, *, transport=None):
        self.auth, self.profile = auth, profile
        p = auth.store.get(profile)
        require(p.get("subject") and p.get("client_id"), "subscription_login_required")
        require(isinstance(p.get("model"), str) and p["model"], "subscription_model_required")
        self.model = p["model"]
        self.account = fingerprint(p)
        self.transport = transport

    @property
    def revision(self):
        self.check_account()
        return "siwc-http-v2:" + self.account

    def request_size(self, payload):
        # A tentative batch may exceed the send cap before the splitter flushes
        # the preceding batch. Measure it with every structural check retained;
        # generate() still uses the size-enforcing request_body default.
        return encoded_size(request_body(payload, self.model, measure_only=True))

    def check_account(self):
        current = self.auth.store.get(self.profile)
        require(
            fingerprint(current) == self.account and current.get("model") == self.model, "subscription_account_changed"
        )

    def fail(self, code, status=None):
        if code in _PAUSE_CODES:
            self.auth.store.pause(self.profile, code)
        if status in (429, 503) and code not in _PAUSE_CODES:
            self.auth.store.pause(self.profile, "subscription_http_" + str(status))
        if code not in _PAUSE_CODES and (status == 401 or code in ("invalid_grant", "invalid_token")):
            self.auth.store.pause(self.profile, "subscription_login_required")
        elif status == 403 and code not in _PAUSE_CODES:
            self.auth.store.pause(self.profile, "subscription_permission_denied")
        if code not in _PAUSE_CODES:
            code = "subscription_response_failed" if status is None else "subscription_http_" + str(status)
        raise ClerkError(code)

    async def generate(self, payload):
        self.check_account()
        body = request_body(payload, self.model)
        token = await asyncio.to_thread(self.auth.access, self.profile, expected=self.account)
        self.check_account()
        async with httpx.AsyncClient(
            timeout=40, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            async with client.stream(
                "POST", RESOURCE + "/responses", json=body, headers={"Authorization": "Bearer " + token}
            ) as response:
                if response.status_code != 200:
                    # Do not copy provider diagnostic prose (may echo private inputs).
                    body = b""
                    async for block in response.aiter_bytes():
                        body += block
                        require(len(body) <= 65536, "subscription_error_response_too_large")
                    try:
                        error = _json(body).get("error") or {}
                        code = error.get("code")
                    except (ClerkError, TypeError, ValueError, AttributeError):
                        code = None
                    self.fail(code, response.status_code)
                done_messages = {}
                added_messages = {}
                response_id = None
                async for event in sse_events(response):
                    require(isinstance(event, dict), "subscription_invalid_response")
                    kind = event.get("type")
                    if event.get("response_id") is not None:
                        require(
                            isinstance(event["response_id"], str)
                            and (response_id is None or response_id == event["response_id"]),
                            "subscription_response_mismatch",
                        )
                        response_id = event["response_id"]
                    if kind in ("response.created", "response.in_progress"):
                        created = event.get("response")
                        require(
                            isinstance(created, dict)
                            and isinstance(created.get("id"), str)
                            and (response_id is None or response_id == created["id"]),
                            "subscription_response_mismatch",
                        )
                        response_id = created["id"]
                    if kind == "response.output_item.added":
                        index, item = event.get("output_index"), event.get("item")
                        require(
                            type(index) is int and 0 <= index < 128 and isinstance(item, dict),
                            "subscription_invalid_response",
                        )
                        if item.get("type") != "reasoning":
                            require(
                                item.get("type") == "message"
                                and item.get("role") == "assistant"
                                and isinstance(item.get("id"), str),
                                "subscription_unexpected_tool_output",
                            )
                            require(
                                index not in added_messages or added_messages[index] == item["id"],
                                "subscription_response_mismatch",
                            )
                            added_messages[index] = item["id"]
                    if kind == "response.output_item.done":
                        index = event.get("output_index")
                        require(type(index) is int and 0 <= index < 128, "subscription_invalid_response")
                        message = final_message(event.get("item"), streamed=True)
                        if message is not None:
                            require(isinstance(message["id"], str) and message["id"], "subscription_invalid_response")
                            require(
                                index not in added_messages or added_messages[index] == message["id"],
                                "subscription_response_mismatch",
                            )
                            require(
                                index not in done_messages or done_messages[index] == message,
                                "subscription_response_mismatch",
                            )
                            require(
                                not any(m["id"] == message["id"] and i != index for i, m in done_messages.items()),
                                "subscription_response_mismatch",
                            )
                            done_messages[index] = message
                            require(
                                sum(len(m["text"].encode()) for m in done_messages.values()) <= 128000,
                                "subscription_response_too_large",
                            )
                    if kind in ("response.failed", "response.incomplete", "error"):
                        error = (event.get("response") or {}).get("error") or event.get("error") or {}
                        code = (
                            event.get("code")
                            if kind == "error"
                            else (error.get("code") if isinstance(error, dict) else None)
                        )
                        if kind == "response.incomplete":
                            raise ClerkError("subscription_response_incomplete")
                        self.fail(code)
                    if kind != "response.completed":
                        continue
                    result = event.get("response")
                    require(
                        isinstance(result, dict) and result.get("status") == "completed",
                        "subscription_invalid_response",
                    )
                    require(response_id is None or result.get("id") == response_id, "subscription_response_mismatch")
                    output = result.get("output")
                    require(output is None or isinstance(output, list), "subscription_invalid_response")
                    if output:
                        final_messages = {
                            i: m for i, item in enumerate(output) if (m := final_message(item)) is not None
                        }
                        require(
                            all(final_messages.get(i) == m for i, m in done_messages.items()),
                            "subscription_response_mismatch",
                        )
                    else:
                        # Some direct-route streams omit the final output snapshot.
                        # Only item.done messages may fill it, never raw deltas.
                        final_messages = done_messages
                    require(
                        bool(final_messages)
                        and all(
                            i in final_messages and final_messages[i]["id"] == mid for i, mid in added_messages.items()
                        ),
                        "subscription_invalid_response",
                    )
                    text = "".join(final_messages[i]["text"] for i in sorted(final_messages))
                    require(len(text.encode()) <= 128000, "subscription_response_too_large")
                    reply = _json(text)
                    require(isinstance(reply, dict) and "usage" not in reply, "subscription_invalid_response")
                    usage = result.get("usage") or {}
                    require(isinstance(usage, dict), "subscription_invalid_usage")
                    report = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in usage}
                    details = usage.get("input_tokens_details") or {}
                    require(isinstance(details, dict), "subscription_invalid_usage")
                    if "cached_tokens" in details:
                        report["cached_input_tokens"] = details["cached_tokens"]
                    require(
                        all(type(v) is int and 0 <= v <= 1000000000 for v in report.values()),
                        "subscription_invalid_usage",
                    )
                    self.check_account()
                    reply["usage"] = report
                    return reply
        raise ClerkError("subscription_stream_interrupted")


def configured_adapter(config):
    cfg = config.get("night_clerk") or {}
    if not parse_bool(cfg.get("enabled"), default=False) or cfg.get("provider") != "chatgpt_subscription":
        return None
    require(cfg.get("input_profile") == "scene_word_mask", "subscription_reviewed_input_profile_required")
    require(isinstance(cfg.get("auth_dir"), str) and cfg["auth_dir"], "subscription_auth_path_required")
    store = AuthStore(cfg["auth_dir"], vault=config.get("buckets_dir"))
    return ChatGPTSubscriptionAdapter(AuthManager(store), cfg.get("profile") or "main")
