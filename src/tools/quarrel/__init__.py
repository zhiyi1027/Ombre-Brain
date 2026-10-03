"""
========================================
tools/quarrel — 没吵完的架（私密延续抽屉的工具入口）
========================================

抽屉本体是 private_continuity：只放一份当前没和好的架，不进搜索、
dream、向量、衰减；breath 睁眼时原文带出来。这里只给模型一个
读 / 写 / 关 的入口，让没有文件系统的客户端（Home SDK）也能用。

关抽屉必须带上她同意和好的原话：原话只写进「上一份」备份（关和存是同一把锁里的
一步），不占抽屉正文的长度，能从 Dashboard 找回。
========================================
"""

from __future__ import annotations

from typing import Any

from private_continuity import PrivateContinuityError

_ACTIONS = {"read", "write", "resolve"}
_MIN_HER_WORDS = 2


def _revision_hint(state: dict[str, Any]) -> str:
    return f"（当前版本 {state.get('revision', 0)}，改的时候把它填进 expected_revision）"


def quarrel(
    service: Any,
    *,
    action: str = "read",
    content: str = "",
    expected_revision: Any = None,
    her_words: str = "",
    source_client: str = "mcp",
) -> str:
    action = str(action or "read").strip().lower()
    if action not in _ACTIONS:
        return "action 只能是 read / write / resolve。"
    if service is None or not getattr(service, "enabled", False):
        return "抽屉没开（private_continuity 未启用）。"

    if action == "read":
        state = service.get_state(include_content=True)
        if not state.get("open"):
            return "抽屉是空的：现在没有没吵完的架。"
        return (
            f"没吵完的架 · 版本 {state.get('revision')} · 最后由 {state.get('source_client') or '?'} "
            f"写于 {state.get('updated_at') or '?'}\n\n{state.get('content') or ''}"
        )

    if action == "write":
        if expected_revision in (None, ""):
            state = service.get_state(include_content=False)
            if state.get("open"):
                return "抽屉里已经有一份了，先 read 看完再改，改的时候带上 expected_revision" + _revision_hint(state)
            expected_revision = 0
        try:
            result = service.upsert(
                content=content,
                source_client=source_client,
                expected_revision=expected_revision,
            )
        except PrivateContinuityError as exc:
            return f"没存进去：{exc}"
        status = {"created": "放进抽屉了", "updated": "改好了", "unchanged": "内容没变"}.get(
            str(result.get("status")), str(result.get("status"))
        )
        return f"{status}，版本 {result.get('revision')}。"

    words = str(her_words or "").strip()
    if len(words) < _MIN_HER_WORDS:
        return "关抽屉要先有她亲口同意和好的那句话，原样填进 her_words。她没说，就不能关。"
    state = service.get_state(include_content=False)
    if not state.get("open"):
        return "抽屉本来就是空的。"
    if expected_revision in (None, ""):
        return "关之前先 read，把版本号填进 expected_revision" + _revision_hint(state)
    try:
        service.resolve(
            source_client=source_client,
            expected_revision=expected_revision,
            resolution_note=f"—— 和好 · 她的原话：{words}",
        )
    except PrivateContinuityError as exc:
        return f"没关上：{exc}"
    return "和好了，抽屉关上。原文连同她那句话留在「上一份」里，Dashboard 能找回。"
