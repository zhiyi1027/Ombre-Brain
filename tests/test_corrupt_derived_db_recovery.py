from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


CORRUPT = {
    "not-a-database": "这根本不是数据库".encode("utf-8") * 50,
    "random-bytes": bytes(range(256)) * 40,
    "sqlite-header-garbage": b"SQLite format 3\x00" + b"\xff" * 4000,
}


def _embedding_config(root: Path) -> dict:
    return {
        "buckets_dir": str(root),
        "embedding": {
            "enabled": True,
            "provider": "openai",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout_seconds": 5,
        },
    }


def _dehydration_config(root: Path) -> dict:
    return {
        "buckets_dir": str(root),
        "dehydration": {
            "enabled": True,
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout_seconds": 5,
        },
    }


def _quarantined(directory: Path, stem: str) -> list[Path]:
    return list(directory.glob(f"{stem}.corrupt-*"))


@pytest.mark.parametrize("blob", CORRUPT.values(), ids=CORRUPT)
def test_corrupt_vector_db_is_quarantined_and_rebuilt(tmp_path, blob):
    from embedding_engine import EmbeddingEngine

    config = _embedding_config(tmp_path)
    db = Path(EmbeddingEngine(config).db_path)
    db.write_bytes(blob)

    engine = EmbeddingEngine(config)

    assert _quarantined(db.parent, db.name)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone() == (0,)
    assert engine.enabled is True


def test_healthy_vector_db_keeps_existing_rows(tmp_path):
    from embedding_engine import EmbeddingEngine

    config = _embedding_config(tmp_path)
    db = Path(EmbeddingEngine(config).db_path)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO embeddings (bucket_id, embedding, updated_at) "
            "VALUES ('b1', '[1]', 'x')"
        )
        conn.commit()

    EmbeddingEngine(config)

    assert not _quarantined(db.parent, db.name)
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT count(*) FROM embeddings").fetchone() == (1,)


@pytest.mark.parametrize("blob", CORRUPT.values(), ids=CORRUPT)
def test_corrupt_dehydration_cache_is_quarantined_and_rebuilt(tmp_path, blob):
    from dehydrator import Dehydrator

    config = _dehydration_config(tmp_path)
    first = Dehydrator(config)
    cache = Path(first.cache_db_path)
    first.close()
    cache.write_bytes(blob)

    second = Dehydrator(config)
    try:
        assert _quarantined(cache.parent, cache.name)
        assert second._cache_conn.execute(
            "SELECT count(*) FROM dehydration_cache"
        ).fetchone() == (0,)
    finally:
        second.close()


def test_healthy_dehydration_cache_keeps_existing_rows(tmp_path):
    from dehydrator import Dehydrator

    config = _dehydration_config(tmp_path)
    first = Dehydrator(config)
    first._cache_conn.execute(
        "INSERT INTO dehydration_cache (content_hash, summary, model) "
        "VALUES ('h', 's', 'm')"
    )
    first._cache_conn.commit()
    cache = Path(first.cache_db_path)
    first.close()

    second = Dehydrator(config)
    try:
        assert not _quarantined(cache.parent, cache.name)
        assert second._cache_conn.execute(
            "SELECT count(*) FROM dehydration_cache"
        ).fetchone() == (1,)
    finally:
        second.close()
