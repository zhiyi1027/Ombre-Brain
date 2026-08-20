import pytest

from ombrebrain.storage.quote_store import (
    MAX_QUOTES,
    MAX_QUOTE_CHARS,
    normalize_quotes,
    quotes_from_metadata,
    render_quotes,
)


def test_plain_strings_and_optional_fields_are_normalized_in_order():
    assert normalize_quotes(
        ["先说的", {"text": "后说的", "speaker": "知知", "at": "2026-08-20"}]
    ) == [
        {"text": "先说的"},
        {"text": "后说的", "speaker": "知知", "at": "2026-08-20"},
    ]


def test_quote_limits_reject_instead_of_truncating():
    with pytest.raises(ValueError, match="最多"):
        normalize_quotes([f"第{i}句" for i in range(MAX_QUOTES + 1)])
    with pytest.raises(ValueError, match="不会截断"):
        normalize_quotes(["字" * (MAX_QUOTE_CHARS + 1)])


def test_bad_stored_quote_does_not_hide_good_entries():
    result = quotes_from_metadata(
        {"quotes": ["好的一句", "字" * 999, 123, {"text": "另一句"}]}
    )

    assert [quote["text"] for quote in result] == ["好的一句", "另一句"]


def test_render_quotes_is_verbatim():
    text = "我不会走的"
    rendered = render_quotes([{"text": text, "speaker": "知知", "at": "昨晚"}])

    assert text in rendered
    assert "知知" in rendered
    assert "昨晚" in rendered

