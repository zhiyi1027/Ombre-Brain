"""Relationship-semantics prompt regressions."""

from types import SimpleNamespace

import pytest

from dehydrator import Dehydrator, _PROMPT_VERSION
from import_memory import ImportEngine
from prompt_rules import relationship_semantics_rule


def _dehydrator(tmp_path) -> Dehydrator:
    return Dehydrator(
        {
            "buckets_dir": str(tmp_path / "vault"),
            "human": "知知",
            "dehydration": {},
        }
    )


def _assert_rule(prompt: str) -> None:
    assert "称呼与真实关系消歧" in prompt
    assert "只能证明一种称呼方式" in prompt
    assert "爸爸亲亲，我是你老婆" in prompt
    assert "不是父女或亲子" in prompt
    assert "我父亲今天陪我去医院" in prompt
    assert "可以标记家庭、父亲或亲子关系" in prompt
    assert "若「知知」是在称呼「我」（AI）" in prompt
    assert "伴侣、亲密、亲昵称呼" in prompt
    assert "domain 描述正文的主事件" in prompt
    assert "不能只因出现老公/老婆、爸爸/宝宝" in prompt
    assert "关系本身（承诺、争吵和好、吃醋、信任、相处变化）" in prompt


def test_shared_rule_distinguishes_nickname_from_real_kinship():
    rule = relationship_semantics_rule("知知")

    _assert_rule(rule)
    assert "不得仅因亲属式称呼选择「家庭」主题域" in rule
    assert "正文、摘要、标题、domain、tags、keywords" in rule


def test_shared_rule_preserves_explicit_adoption_relationships():
    rule = relationship_semantics_rule("知知")

    assert "收养" in rule
    assert "从小由养父抚养" in rule
    assert "才允许生成对应的亲属关系描述" in rule


def test_shared_rule_preserves_explicit_fictional_relationships():
    rule = relationship_semantics_rule("知知")

    assert "明确说明作品中" in rule
    assert "人物确有该设定" in rule
    assert "才允许生成对应的亲属关系描述" in rule


@pytest.mark.asyncio
async def test_all_dehydrator_memory_prompts_include_relationship_rule(
    tmp_path, monkeypatch
):
    dehydrator = _dehydrator(tmp_path)
    prompts = []

    async def fake_chat(system: str, _user: str, **_kwargs):
        prompts.append(system)
        if "内容分析器" in system:
            return '{"domain":["伴侣"],"valence":0.8,"arousal":0.5,"tags":["亲昵称呼"],"suggested_name":"昵称"}'
        if "日记整理专家" in system:
            return "[]"
        return "merged-or-dehydrated"

    monkeypatch.setattr(dehydrator, "_chat", fake_chat)

    await dehydrator._api_dehydrate("长记忆")
    await dehydrator._api_merge("旧记忆", "新记忆")
    await dehydrator._api_analyze("爸爸亲亲，我是你老婆")
    await dehydrator._api_digest("今天叫了恋人爸爸")
    dehydrator.close()

    assert _PROMPT_VERSION >= 6
    assert len(prompts) == 4
    for prompt in prompts:
        _assert_rule(prompt)


def test_all_domain_prompts_use_the_specific_relationship_taxonomy():
    from dehydrator import ANALYZE_PROMPT, DIGEST_PROMPT
    from import_memory import IMPORT_EXTRACT_PROMPT

    expected = '关系: ["家庭", "伴侣", "亲密", "性", "友谊", "社交"]'
    for prompt in (ANALYZE_PROMPT, DIGEST_PROMPT, IMPORT_EXTRACT_PROMPT):
        assert expected in prompt
        assert '人际: ["家庭", "恋爱", "友谊", "社交"]' not in prompt


@pytest.mark.asyncio
async def test_import_extraction_includes_the_same_relationship_rule(monkeypatch):
    captured = {}

    async def fake_chat(system: str, _user: str, **_kwargs):
        captured["system"] = system
        return "[]"

    engine = object.__new__(ImportEngine)
    engine.config = {"human": "知知"}
    engine.dehydrator = SimpleNamespace(api_available=True, _chat=fake_chat)

    assert await engine._extract_memories("知知：爸爸亲亲，我是你老婆") == []
    _assert_rule(captured["system"])
