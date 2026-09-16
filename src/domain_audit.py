#!/usr/bin/env python3
"""Read-only audit for legacy and inconsistent memory domains.

This module deliberately has no apply path.  It reads bucket frontmatter and
emits a deterministic JSON preview without changing bucket content, metadata,
paths, embeddings, or indexes.  Ambiguous cases stay marked for content review
instead of being guessed from a title.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import frontmatter
import yaml


SCHEMA_VERSION = 1
MAX_DOMAINS = 2
AUDITED_BUCKET_TYPES = frozenset({"dynamic", "permanent"})
SYSTEM_BUCKET_TYPES = frozenset({"feel", "plan", "letter", "self", "i"})
UNCLASSIFIED_DOMAINS = frozenset({"未分类", "unclassified"})

CANONICAL_DOMAINS = frozenset(
    {
        "饮食",
        "穿搭",
        "出行",
        "居家",
        "购物",
        "家庭",
        "友谊",
        "社交",
        "工作",
        "学习",
        "考试",
        "求职",
        "健康",
        "心理",
        "睡眠",
        "运动",
        "游戏",
        "影视",
        "音乐",
        "阅读",
        "创作",
        "手工",
        "编程",
        "AI",
        "硬件",
        "网络",
        "财务",
        "计划",
        "待办",
        "情绪",
        "回忆",
        "梦境",
        "自省",
        # Useful local extensions that are intentionally more precise than 恋爱.
        "亲密",
        "性",
    }
)

PARENT_DOMAINS = frozenset(
    {
        "日常",
        "关系",
        "人际",
        "恋爱",
        "成长",
        "身心",
        "兴趣",
        "数字",
        "事务",
        "内心",
    }
)

DOMAIN_ALIASES = {
    "技术": "编程",
    "投资": "财务",
    "文学": "阅读",
    "亲密互动": "亲密",
    "性爱": "性",
    "情感": "情绪",
    "感受": "情绪",
    "self": "自省",
}

# These are useful retrieval facets, but they are too narrow to be navigation
# domains.  A future confirmed apply step can preserve them as tags.
FACET_DOMAINS = frozenset(
    {
        "安全感",
        "称呼",
        "冲突",
        "故障",
        "陪伴",
        "共同生活",
        "沟通",
        "纪念日",
        "记忆",
        "认知",
        "身份",
        "声音",
        "信任",
    }
)


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        candidates: Iterable[object] = [value]
    elif isinstance(value, set):
        candidates = sorted(value, key=lambda item: str(item))
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = []
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _bucket_type(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("type") or "dynamic").strip().lower()


def _config_path_read_only() -> Path:
    """Resolve the existing config path without migrations or directory writes."""

    explicit = os.environ.get("OMBRE_CONFIG_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    project_root = Path(__file__).resolve().parent.parent
    cwd_config = Path.cwd() / "config.yaml"
    data_dir = (
        os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
        or os.environ.get("OMBRE_VAULT_DIR", "").strip()
    )
    is_render = os.environ.get("RENDER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if is_render and data_dir:
        persistent_config = Path(data_dir).expanduser().absolute() / "config.yaml"
        if persistent_config.is_file():
            return persistent_config
    if cwd_config.is_file():
        return cwd_config
    return project_root / "config.yaml"


def _buckets_root_read_only(
    config: Mapping[str, Any] | None,
    buckets_dir: str | Path | None,
) -> Path:
    """Resolve the vault root without calling the mutating runtime loader."""

    if buckets_dir is not None:
        return Path(buckets_dir).expanduser()
    if config is not None:
        configured = str(config.get("buckets_dir") or "").strip()
        if configured:
            return Path(configured).expanduser()

    env_buckets = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    env_vault = os.environ.get("OMBRE_VAULT_DIR", "").strip()
    if env_buckets or env_vault:
        # Match load_config(): the legacy variable is applied first, and the
        # later vault alias only overrides it when OMBRE_BUCKETS_DIR is absent.
        return Path(env_buckets or env_vault).expanduser()

    config_path = _config_path_read_only()
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                persisted = yaml.safe_load(handle) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            persisted = {}
        if isinstance(persisted, Mapping):
            configured = str(persisted.get("buckets_dir") or "").strip()
            if configured:
                return Path(configured).expanduser()

    return Path(__file__).resolve().parent.parent / "buckets"


def audit_bucket(
    metadata: Mapping[str, Any],
    content: str,
    *,
    source_path: str = "",
) -> dict[str, Any] | None:
    """Return one preview candidate, or ``None`` for a clean/system bucket."""

    bucket_type = _bucket_type(metadata)
    if bucket_type in SYSTEM_BUCKET_TYPES or bucket_type not in AUDITED_BUCKET_TYPES:
        return None

    current_domains = _string_list(metadata.get("domain")) or ["未分类"]
    current_tags = _string_list(metadata.get("tags"))
    proposed_domains: list[str] = []
    proposed_tags = list(current_tags)
    flags: list[str] = []
    reasons: list[str] = []

    if any(domain.lower() in UNCLASSIFIED_DOMAINS for domain in current_domains):
        flags.append("unclassified")
        reasons.append("缺少可用主题域，必须读正文后再分类")

    if len(current_domains) > MAX_DOMAINS:
        flags.append("too_many_domains")
        reasons.append(f"主题域超过 {MAX_DOMAINS} 个，需要判断主次")

    if any(domain in PARENT_DOMAINS for domain in current_domains):
        flags.append("parent_domain")
        reasons.append("使用了导航大类名，需要结合正文选择具体小类")

    if "家庭" in current_domains and "恋爱" in current_domains:
        flags.append("family_romance_ambiguous")
        reasons.append("家庭与恋爱并存，需排除亲昵称呼造成的旧版误判")

    for domain in current_domains:
        if domain.lower() in UNCLASSIFIED_DOMAINS:
            _append_unique(proposed_domains, "未分类")
        elif domain in PARENT_DOMAINS:
            _append_unique(proposed_domains, domain)
        elif domain in DOMAIN_ALIASES:
            _append_unique(proposed_domains, DOMAIN_ALIASES[domain])
            if "legacy_alias" not in flags:
                flags.append("legacy_alias")
                reasons.append("存在可确定归一的旧域别名")
        elif domain in FACET_DOMAINS:
            _append_unique(proposed_tags, domain)
            if "facet_domain" not in flags:
                flags.append("facet_domain")
                reasons.append("细粒度概念更适合作为可搜索标签")
        elif domain in CANONICAL_DOMAINS:
            _append_unique(proposed_domains, domain)
        else:
            _append_unique(proposed_domains, domain)
            if "custom_domain" not in flags:
                flags.append("custom_domain")
                reasons.append("发现词表外自定义域，保留原值并等待人工确认")

    if not flags:
        return None

    requires_content_review = any(
        flag
        in {
            "unclassified",
            "too_many_domains",
            "parent_domain",
            "family_romance_ambiguous",
            "custom_domain",
        }
        for flag in flags
    )
    if not proposed_domains:
        proposed_domains = list(current_domains)
        requires_content_review = True

    confidence = "review" if requires_content_review else "high"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "bucket_id": str(metadata.get("id") or "").strip(),
        "name": str(metadata.get("name") or "").strip(),
        "source_path": source_path,
        "bucket_type": bucket_type,
        "content_sha256": content_hash,
        "current": {"domains": current_domains, "tags": current_tags},
        "proposal": {
            "domains": proposed_domains,
            "tags": proposed_tags,
        },
        "flags": flags,
        "reasons": reasons,
        "confidence": confidence,
        "requires_content_review": requires_content_review,
    }


def audit_vault(
    config: Mapping[str, Any] | None = None,
    *,
    buckets_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Scan a vault and return a deterministic, content-free preview report."""

    root = _buckets_root_read_only(config, buckets_dir)
    scan_roots = (root / "dynamic", root / "permanent")
    files = sorted(
        path
        for scan_root in scan_roots
        if scan_root.is_dir()
        for path in scan_root.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    )
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped_system = 0
    skipped_other = 0
    eligible = 0

    for path in files:
        relative = str(path.relative_to(root))
        try:
            post = frontmatter.load(path)
            bucket_type = _bucket_type(post.metadata)
            if bucket_type in SYSTEM_BUCKET_TYPES:
                skipped_system += 1
                continue
            if bucket_type not in AUDITED_BUCKET_TYPES:
                skipped_other += 1
                continue
            eligible += 1
            candidate = audit_bucket(
                post.metadata,
                post.content,
                source_path=relative,
            )
            if candidate is not None:
                candidates.append(candidate)
        except Exception as exc:
            errors.append(
                {
                    "source_path": relative,
                    "error_type": type(exc).__name__,
                    "error": "failed to parse bucket frontmatter",
                }
            )

    candidates.sort(
        key=lambda item: (
            str(item.get("bucket_id") or ""),
            str(item.get("source_path") or ""),
        )
    )
    flag_counts = Counter(
        flag for candidate in candidates for flag in candidate.get("flags", [])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "read_only_preview",
        "root": str(root),
        "summary": {
            "scanned_files": len(files),
            "eligible_buckets": eligible,
            "skipped_system_buckets": skipped_system,
            "skipped_other_buckets": skipped_other,
            "candidate_buckets": len(candidates),
            "high_confidence_candidates": sum(
                item["confidence"] == "high" for item in candidates
            ),
            "content_review_candidates": sum(
                bool(item["requires_content_review"]) for item in candidates
            ),
            "parse_errors": len(errors),
            "flag_counts": dict(sorted(flag_counts.items())),
        },
        "candidates": candidates,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only preview of legacy and inconsistent memory domains."
    )
    parser.add_argument(
        "--buckets-dir",
        help="Override config buckets_dir for this read-only scan.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit_vault(buckets_dir=args.buckets_dir)
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
