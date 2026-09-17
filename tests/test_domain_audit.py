import hashlib
import json
from pathlib import Path

import frontmatter
import pytest

from domain_audit import audit_bucket, audit_vault, build_parser, main


def _write_bucket(
    root: Path,
    relative: str,
    *,
    bucket_id: str,
    bucket_type: str = "dynamic",
    domains: list[str] | None = None,
    tags: list[str] | None = None,
    content: str = "原文必须逐字保持。",
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content,
        id=bucket_id,
        name=f"桶-{bucket_id}",
        type=bucket_type,
        domain=domains if domains is not None else ["恋爱"],
        tags=tags or [],
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def test_audit_bucket_proposes_only_deterministic_alias_and_facet_changes():
    content = "这段正文不会进入报告，也不会被改写。"
    candidate = audit_bucket(
        {
            "id": "abc123",
            "name": "旧域",
            "type": "dynamic",
            "domain": ["家庭", "技术", "身份"],
            "tags": ["原标签"],
        },
        content,
        source_path="dynamic/家庭/abc123.md",
    )

    assert candidate is not None
    assert candidate["current"]["domains"] == ["家庭", "技术", "身份"]
    assert candidate["proposal"]["domains"] == ["家庭", "编程"]
    assert candidate["proposal"]["tags"] == ["原标签", "身份"]
    assert candidate["flags"] == [
        "too_many_domains",
        "legacy_alias",
        "facet_domain",
    ]
    assert candidate["confidence"] == "review"
    assert candidate["requires_content_review"] is True
    assert candidate["content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert content not in json.dumps(candidate, ensure_ascii=False)


def test_audit_bucket_keeps_ambiguous_and_custom_domains_for_review():
    candidate = audit_bucket(
        {
            "id": "ambiguous",
            "type": "permanent",
            "domain": ["家庭", "恋爱", "旧分类", "内心"],
            "tags": [],
        },
        "正文",
    )

    assert candidate is not None
    assert candidate["proposal"]["domains"] == [
        "家庭",
        "伴侣",
        "旧分类",
        "内心",
    ]
    assert set(candidate["flags"]) == {
        "too_many_domains",
        "parent_domain",
        "legacy_romance_review",
        "family_romance_ambiguous",
        "custom_domain",
    }
    assert candidate["confidence"] == "review"


def test_audit_bucket_treats_dashboard_group_names_as_parent_domains():
    for domain in (
        "日常",
        "关系",
        "成长",
        "身心",
        "兴趣",
        "数字",
        "事务",
        "内心",
    ):
        candidate = audit_bucket(
            {"id": domain, "type": "dynamic", "domain": [domain]},
            "正文",
        )

        assert candidate is not None
        assert candidate["flags"] == ["parent_domain"]
        assert candidate["proposal"]["domains"] == [domain]


def test_audit_bucket_proposes_companion_as_review_only_romance_fallback():
    candidate = audit_bucket(
        {
            "id": "legacy-romance",
            "type": "dynamic",
            "domain": ["恋爱"],
        },
        "正文仍需人工判断真实主题",
    )

    assert candidate is not None
    assert candidate["proposal"]["domains"] == ["伴侣"]
    assert candidate["flags"] == ["legacy_romance_review"]
    assert candidate["confidence"] == "review"
    assert candidate["requires_content_review"] is True
    assert "兜底建议" in candidate["reasons"][0]


def test_audit_bucket_accepts_companion_as_a_clean_specific_domain():
    assert (
        audit_bucket(
            {"id": "companion", "type": "dynamic", "domain": ["伴侣"]},
            "正文",
        )
        is None
    )


def test_audit_bucket_normalizes_legacy_unclassified_sentinel():
    candidate = audit_bucket(
        {
            "id": "legacy-unclassified",
            "type": "dynamic",
            "domain": ["unclassified"],
        },
        "正文",
    )

    assert candidate is not None
    assert candidate["flags"] == ["unclassified"]
    assert candidate["proposal"]["domains"] == ["未分类"]
    assert candidate["requires_content_review"] is True


def test_audit_bucket_skips_clean_and_system_buckets():
    assert (
        audit_bucket(
            {"id": "clean", "type": "dynamic", "domain": ["家庭", "自省"]},
            "正文",
        )
        is None
    )
    for bucket_type in ("feel", "plan", "letter", "self", "i", "archived"):
        assert (
            audit_bucket(
                {"id": bucket_type, "type": bucket_type, "domain": ["未分类"]},
                "正文",
            )
            is None
        )


def test_audit_vault_is_byte_identical_and_deterministic(tmp_path):
    root = tmp_path / "buckets"
    alias_path = _write_bucket(
        root,
        "dynamic/技术/alias.md",
        bucket_id="b-alias",
        domains=["技术"],
    )
    ambiguous_path = _write_bucket(
        root,
        "dynamic/未分类/missing.md",
        bucket_id="a-missing",
        domains=["未分类"],
    )
    _write_bucket(
        root,
        "dynamic/private.md",
        bucket_id="feel",
        bucket_type="feel",
        domains=["未分类"],
    )
    artifact = _write_bucket(
        root,
        "daily_continuity/2026-09-16.md",
        bucket_id="not-a-bucket",
        domains=["未分类"],
    )
    before = {path: path.read_bytes() for path in root.rglob("*.md")}

    first = audit_vault({}, buckets_dir=root)
    second = audit_vault({}, buckets_dir=root)

    assert first == second
    assert first["mode"] == "read_only_preview"
    assert first["summary"] == {
        "scanned_files": 3,
        "eligible_buckets": 2,
        "skipped_system_buckets": 1,
        "skipped_other_buckets": 0,
        "candidate_buckets": 2,
        "high_confidence_candidates": 1,
        "content_review_candidates": 1,
        "parse_errors": 0,
        "flag_counts": {"legacy_alias": 1, "unclassified": 1},
    }
    assert [item["bucket_id"] for item in first["candidates"]] == [
        "a-missing",
        "b-alias",
    ]
    assert {path: path.read_bytes() for path in root.rglob("*.md")} == before
    assert alias_path.exists()
    assert ambiguous_path.exists()
    assert artifact.exists()
    assert list(root.rglob("*.json")) == []


def test_cli_has_no_apply_option_and_prints_preview(tmp_path, capsys):
    root = tmp_path / "buckets"
    _write_bucket(
        root,
        "dynamic/技术/alias.md",
        bucket_id="alias",
        domains=["技术"],
    )
    parser = build_parser()
    assert "--apply" not in parser.format_help()

    assert main(["--buckets-dir", str(root), "--compact"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "read_only_preview"
    assert report["summary"]["candidate_buckets"] == 1


def test_audit_vault_does_not_echo_content_from_parse_errors(tmp_path):
    root = tmp_path / "buckets"
    broken = root / "dynamic" / "broken.md"
    broken.parent.mkdir(parents=True)
    secret = "PRIVATE MEMORY BODY MUST NOT LEAK"
    broken.write_text(f"---\ndomain: [broken\n---\n{secret}\n", encoding="utf-8")

    report = audit_vault({}, buckets_dir=root)
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["summary"]["parse_errors"] == 1
    assert report["errors"] == [
        {
            "source_path": "dynamic/broken.md",
            "error_type": "ParserError",
            "error": "failed to parse bucket frontmatter",
        }
    ]
    assert secret not in rendered


def test_default_config_resolution_does_not_create_vault_directories(
    tmp_path, monkeypatch
):
    missing_vault = tmp_path / "missing-vault"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"buckets_dir: {missing_vault}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("OMBRE_BUCKETS_DIR", raising=False)
    monkeypatch.delenv("OMBRE_VAULT_DIR", raising=False)

    report = audit_vault()

    assert report["root"] == str(missing_vault)
    assert report["summary"]["scanned_files"] == 0
    assert not missing_vault.exists()


def test_legacy_bucket_env_wins_when_both_vault_variables_are_set(
    tmp_path, monkeypatch
):
    legacy_vault = tmp_path / "legacy-vault"
    preferred_vault = tmp_path / "preferred-vault"
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(legacy_vault))
    monkeypatch.setenv("OMBRE_VAULT_DIR", str(preferred_vault))

    report = audit_vault()

    assert report["root"] == str(legacy_vault)
    assert not legacy_vault.exists()
    assert not preferred_vault.exists()


@pytest.mark.parametrize(
    "config_text",
    ["buckets_dir: [broken\n", "- not\n- a\n- mapping\n"],
)
def test_invalid_config_falls_back_without_mutation(tmp_path, monkeypatch, config_text):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    before = config_path.read_bytes()
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("OMBRE_BUCKETS_DIR", raising=False)
    monkeypatch.delenv("OMBRE_VAULT_DIR", raising=False)

    report = audit_vault()

    assert report["mode"] == "read_only_preview"
    assert config_path.read_bytes() == before
