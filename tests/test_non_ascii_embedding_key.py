from __future__ import annotations

import pytest

import errors
from embedding_engine import EmbeddingEngine, _first_non_ascii, _header_safe


GOOD_KEY = "AIzaSyC" + "x" * 32
CONTAMINATED = "AI密钥Sy" + "x" * 30


def _engine(tmp_path, api_key):
    return EmbeddingEngine(
        {
            "buckets_dir": str(tmp_path),
            "embedding": {
                "enabled": True,
                "provider": "openai",
                "api_format": "openai_compat",
                "base_url": "https://example.invalid/v1",
                "api_key": api_key,
                "model": "m",
                "timeout_seconds": 5,
            },
        }
    )


@pytest.fixture(autouse=True)
def error_log(tmp_path):
    errors.configure_errors_path(str(tmp_path / "errors"))


@pytest.mark.parametrize("value", [GOOD_KEY, "sk-abc123", "ollama", ""])
def test_ascii_keys_are_header_safe(value):
    assert _header_safe(value) is True


@pytest.mark.parametrize(
    "value",
    [CONTAMINATED, "密钥", "sk-abc－123", "key　with　space"],
)
def test_non_ascii_keys_are_refused(value):
    assert _header_safe(value) is False


def test_bad_character_position_is_one_based():
    assert _first_non_ascii(CONTAMINATED) == 3
    assert _first_non_ascii("密钥") == 1
    assert _first_non_ascii(GOOD_KEY) == 0


def test_contaminated_key_enters_standby_and_keeps_store(tmp_path):
    engine = _engine(tmp_path, CONTAMINATED)

    assert engine.enabled is False
    assert engine.db_path


def test_recorded_error_identifies_key_and_position(tmp_path):
    _engine(tmp_path, CONTAMINATED)

    detail = " ".join(str(item.get("detail")) for item in errors.recent_errors(limit=5))
    assert "api_key" in detail
    assert "position 3" in detail
    assert "UnicodeEncodeError" not in detail


@pytest.mark.asyncio
async def test_vector_write_degrades_without_retrying(tmp_path):
    engine = _engine(tmp_path, CONTAMINATED)

    assert await engine.generate_and_store("b1", "今天下午聊了很久") is False
