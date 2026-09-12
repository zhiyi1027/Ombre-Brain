from __future__ import annotations

import asyncio

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError
from openai._models import FinalRequestOptions

import dehydrator as dehydrator_module
from dehydrator import Dehydrator, _RETRY_MAX_ATTEMPTS
from embedding_engine import APIEmbeddingEngine, _API_TIMEOUT_SECONDS


REQUEST = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _status_error(code):
    response = httpx.Response(code, request=REQUEST)
    return httpx.HTTPStatusError("boom", request=REQUEST, response=response)


@pytest.fixture
def dehydrator(tmp_path):
    instance = Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {
                "enabled": True,
                "api_format": "openai_compat",
                "base_url": "https://example.invalid/v1",
                "api_key": "k",
                "model": "m",
                "timeout_seconds": 5,
            },
        }
    )
    yield instance
    instance.close()


def test_sdk_does_not_retry_under_application_chat_loop(dehydrator):
    assert dehydrator.client.max_retries == 0


async def _count_attempts(dehydrator, monkeypatch, exc):
    calls = []

    async def boom(*_args, **_kwargs):
        calls.append(1)
        raise exc

    monkeypatch.setattr(dehydrator, "_chat_once", boom)
    monkeypatch.setattr(dehydrator_module, "_RETRY_BASE_DELAY", 0.0)
    with pytest.raises(type(exc)):
        await dehydrator._chat("system", "user")
    return len(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        APIConnectionError(request=REQUEST),
        APITimeoutError(request=REQUEST),
        RateLimitError(
            "slow",
            response=httpx.Response(429, request=REQUEST),
            body=None,
        ),
        _status_error(500),
        _status_error(503),
    ],
)
async def test_transient_errors_use_configured_attempt_count(
    dehydrator, monkeypatch, exc
):
    assert await _count_attempts(dehydrator, monkeypatch, exc) == _RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ValueError("bad request"), _status_error(400)])
async def test_permanent_errors_are_not_retried(dehydrator, monkeypatch, exc):
    assert await _count_attempts(dehydrator, monkeypatch, exc) == 1


def _embedding_engine(timeout_seconds=5.0):
    return APIEmbeddingEngine(
        api_key="k",
        base_url="https://example.invalid/v1",
        model="m",
        timeout_seconds=timeout_seconds,
    )


def _request_timeout(engine):
    options = FinalRequestOptions.construct(
        method="post", url="/embeddings", json_data={}
    )
    return engine._client._build_request(options).extensions["timeout"]


@pytest.mark.parametrize("configured", [5.0, 7.5, 30.0, 120.0])
def test_embedding_timeout_reaches_every_request(configured):
    engine = _embedding_engine(configured)
    try:
        timeout = _request_timeout(engine)
        for phase in ("connect", "read", "write", "pool"):
            assert timeout[phase] == pytest.approx(configured)
    finally:
        asyncio.run(engine._client.close())


def test_invalid_embedding_timeout_uses_default():
    engine = _embedding_engine(0)
    try:
        assert _request_timeout(engine)["read"] == pytest.approx(_API_TIMEOUT_SECONDS)
    finally:
        asyncio.run(engine._client.close())
