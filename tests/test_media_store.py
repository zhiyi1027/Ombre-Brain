"""媒体持久化回归：临时来源必须复制进 OB 持久目录。"""

import base64
from pathlib import Path

import pytest

from media_store import MediaPersistenceError, MediaStore


@pytest.mark.asyncio
async def test_server_readable_temporary_file_is_copied(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = tmp_path / "client-temp.png"
    source.write_bytes(b"image-bytes")
    store = MediaStore(str(vault), str(vault / "_media"))

    result = await store.persist("bucket-1", str(source))

    stored = vault / result[0]["path"]
    assert stored.read_bytes() == b"image-bytes"
    assert result[0]["stored"] is True
    assert result[0]["path"].startswith("_media/bucket-1/")
    source.unlink()
    assert stored.read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_base64_media_is_persisted_with_original_suffix(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    payload = base64.b64encode(b"sound-bytes").decode("ascii")

    result = await store.persist(
        "bucket-2",
        [{"data_base64": payload, "filename": "voice.ogg", "type": "audio/ogg"}],
    )

    stored = vault / result[0]["path"]
    assert stored.suffix == ".ogg"
    assert stored.read_bytes() == b"sound-bytes"


@pytest.mark.asyncio
async def test_unreadable_client_temporary_path_is_rejected(tmp_path: Path) -> None:
    store = MediaStore(str(tmp_path / "vault"), str(tmp_path / "vault" / "_media"))

    with pytest.raises(MediaPersistenceError, match="data_base64"):
        await store.persist("bucket-3", "/client-only/temporary/photo.png")


@pytest.mark.asyncio
async def test_persisted_png_can_be_read_back(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    payload = base64.b64encode(png).decode("ascii")
    stored = await store.persist(
        "bucket-4",
        [{"data_base64": payload, "filename": "photo.png", "type": "image/png"}],
    )

    data, image_format = store.read_image(stored[0]["path"])

    assert data == png
    assert image_format == "png"


def test_read_image_rejects_path_outside_media_dir(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    outside = vault / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(MediaPersistenceError, match="越界"):
        store.read_image("outside.png")


def test_read_image_rejects_symlink(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    target = vault / "_media" / "real.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    link = vault / "_media" / "link.png"
    link.symlink_to(target)

    with pytest.raises(MediaPersistenceError, match="符号链接"):
        store.read_image("_media/link.png")


def test_read_image_rejects_non_image_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = MediaStore(str(vault), str(vault / "_media"))
    bogus = vault / "_media" / "not-really.png"
    bogus.write_bytes(b"plain text")

    with pytest.raises(MediaPersistenceError, match="不是受支持的图片"):
        store.read_image("_media/not-really.png")
