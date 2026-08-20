from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from ombrebrain.storage.quote_store import MAX_QUOTE_CHARS
from tools.breath import dispatch as breath_dispatch
from tools.hold import dispatch as hold_dispatch


BODY = "那天晚上她站在门口，很久没有说话。"
QUOTE = "我不会走的"


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class _StubDehydrator:
    async def analyze(self, _content):
        return {
            "domain": ["关系"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "门口",
        }


class _DisabledEmbedding:
    enabled = False


def _install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}, "limits": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = _NoopDecay()
    rt.dehydrator = _StubDehydrator()
    rt.embedding_engine = _DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


@pytest.mark.asyncio
async def test_quote_stays_out_of_body_and_default_reads(bucket_mgr):
    _install_runtime(bucket_mgr)
    await hold_dispatch(content=BODY, importance=6, quotes=[QUOTE])
    stored = (await bucket_mgr.list_all(include_archive=False))[0]

    assert stored["metadata"]["quotes"] == [{"text": QUOTE}]
    assert QUOTE not in stored["content"]

    default_read = await breath_dispatch(query=stored["id"])
    assert BODY in default_read
    assert QUOTE not in default_read


@pytest.mark.asyncio
async def test_quote_appears_only_after_matching_search_explicitly_requests_it(bucket_mgr):
    _install_runtime(bucket_mgr)
    await hold_dispatch(
        content=BODY,
        importance=6,
        quotes=[{"text": QUOTE, "speaker": "知知"}],
    )
    stored = (await bucket_mgr.list_all(include_archive=False))[0]

    without = await breath_dispatch(query=stored["id"])
    with_quotes = await breath_dispatch(query=stored["id"], quotes=True)

    assert QUOTE not in without
    assert BODY in without
    assert BODY in with_quotes
    assert QUOTE in with_quotes
    assert "知知" in with_quotes


@pytest.mark.asyncio
async def test_invalid_quote_rejects_the_entire_hold(bucket_mgr):
    _install_runtime(bucket_mgr)

    output = await hold_dispatch(
        content=BODY,
        importance=6,
        quotes=["字" * (MAX_QUOTE_CHARS + 1)],
    )

    assert "未创建任何桶" in output
    assert await bucket_mgr.list_all(include_archive=False) == []


@pytest.mark.asyncio
async def test_exact_duplicate_can_add_a_later_deliberate_quote(bucket_mgr):
    _install_runtime(bucket_mgr)
    await hold_dispatch(content=BODY, importance=6)
    await hold_dispatch(content=BODY, importance=6, quotes=[QUOTE])

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["quotes"] == [{"text": QUOTE}]
