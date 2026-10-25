"""Real image fixtures exercise attachment storage without any platform connection."""
import io
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app import chat_attachments
from backend.app.chat_attachments import resolve_attachment, save_attachment
from backend.app.channels.media import media_filename


def image_bytes(format: str, *, animated: bool = False) -> bytes:
    output = io.BytesIO()
    first = Image.new("RGB", (3, 3), "red")
    options = {"save_all": True, "append_images": [Image.new("RGB", (3, 3), "blue")],
               "duration": 80, "loop": 0} if animated else {}
    first.save(output, format=format, **options)
    return output.getvalue()


@pytest.mark.parametrize("format,suffix,mime", [
    ("PNG", ".png", "image/png"), ("JPEG", ".jpg", "image/jpeg"),
    ("WEBP", ".webp", "image/webp"), ("GIF", ".gif", "image/gif"),
])
def test_real_images_roundtrip_as_original_bytes(tmp_path, format, suffix, mime):
    raw = image_bytes(format, animated=format == "GIF")
    item = save_attachment(tmp_path, "sticker" + suffix, mime, raw)
    resolved = resolve_attachment(tmp_path, item["id"])
    assert resolved["kind"] == "image"
    assert resolved["mediaType"] == mime
    assert Path(resolved["path"]).read_bytes() == raw
    assert media_filename("sticker", mime) == "sticker" + suffix
    with Image.open(resolved["path"]) as image:
        assert image.format == format
        assert getattr(image, "n_frames", 1) == (2 if format == "GIF" else 1)


@pytest.mark.parametrize("raw", [b"GIF89a", b"GIF87a" + bytes(40), b"not an image"])
def test_invalid_gif_never_persists(tmp_path, raw):
    with pytest.raises(HTTPException) as error:
        save_attachment(tmp_path, "bad.gif", "image/gif", raw)
    assert error.value.status_code == 415
    assert not (tmp_path / "attachments").exists()


def test_oversized_gif_never_persists(tmp_path, monkeypatch):
    raw = image_bytes("GIF", animated=True)
    monkeypatch.setattr(chat_attachments, "MAX_ATTACHMENT_BYTES", len(raw) - 1)
    with pytest.raises(HTTPException) as error:
        save_attachment(tmp_path, "large.gif", "image/gif", raw)
    assert error.value.status_code == 413
    assert not (tmp_path / "attachments").exists()


def test_gif_rejects_mismatched_mime(tmp_path):
    with pytest.raises(HTTPException) as error:
        save_attachment(tmp_path, "wrong.gif", "image/png", image_bytes("GIF"))
    assert error.value.status_code == 415
    assert not (tmp_path / "attachments").exists()


def test_corrupt_later_gif_frame_never_persists(tmp_path):
    raw = image_bytes("GIF", animated=True)[:-8]
    with pytest.raises(HTTPException) as error:
        save_attachment(tmp_path, "truncated.gif", "image/gif", raw)
    assert error.value.status_code == 415
    assert not (tmp_path / "attachments").exists()


def test_gif_decode_budget_counts_all_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_attachments, "MAX_GIF_DECODED_PIXELS", 17)
    with pytest.raises(HTTPException) as error:
        save_attachment(tmp_path, "large-animation.gif", "image/gif", image_bytes("GIF", animated=True))
    assert error.value.status_code == 415
    assert not (tmp_path / "attachments").exists()


def test_static_gif87a_retains_original(tmp_path):
    raw = image_bytes("GIF")
    assert raw.startswith(b"GIF87a")
    item = save_attachment(tmp_path, "static.gif", "application/octet-stream", raw)
    assert Path(item["path"]).read_bytes() == raw
