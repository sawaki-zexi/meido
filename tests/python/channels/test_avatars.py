from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from backend.app.channels.avatars import download_avatar, thumbnail
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, key):
        return ""

    def set(self, key, value):
        pass


def picture() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (300, 200), "blue").save(output, format="PNG")
    return output.getvalue()


def store_at(tmp_path):
    return MultichannelStore(tmp_path / "sessions.db", SessionStore(tmp_path / "sessions.db"), Credentials())


def test_avatar_is_validated_and_reduced_to_a_png_thumbnail():
    with Image.open(io.BytesIO(thumbnail(picture()))) as image:
        assert image.format == "PNG"
        assert max(image.size) == 128
    with pytest.raises((ValueError, OSError)):
        thumbnail(b"<svg onload='bad()'></svg>")


def test_avatar_cache_coalesces_refresh_and_preserves_previous_photo_after_failure(tmp_path):
    async def scenario():
        store = store_at(tmp_path)
        account = store.register_channel_account("r1", "qq", account_id="qq-first")
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return picture()

        first = store.avatars.refresh(account, "sender", "owner", fetch)
        second = store.avatars.refresh(account, "sender", "owner", fetch)
        assert second is first
        await first
        assert calls == 1
        assert store.avatars.url(account.account_id, "sender", "owner")
        assert store.avatars.refresh(account, "sender", "owner", fetch) is None
        key = store.avatars.key(account.account_id, "sender", "owner")
        previous = store.avatars.path(key).read_bytes()
        with store._connect() as db:
            db.execute("UPDATE channel_avatars SET attempted_at='2020-01-01T00:00:00+00:00'")

        async def fail():
            raise RuntimeError("network error")

        await store.avatars.refresh(account, "sender", "owner", fail)
        assert store.avatars.path(key).read_bytes() == previous
    asyncio.run(scenario())


def test_avatar_cache_is_application_scoped_and_role_deletion_cleans_only_its_files(tmp_path):
    async def scenario():
        store = store_at(tmp_path)
        accounts = [store.register_channel_account(role, "feishu", account_id=f"feishu-{role}") for role in ("r1", "r2")]

        async def fetch():
            return picture()

        for account in accounts:
            await store.avatars.refresh(account, "sender", "same-open-id", fetch)
        paths = [store.avatars.path(store.avatars.key(account.account_id, "sender", "same-open-id")) for account in accounts]
        assert paths[0] != paths[1]
        store.delete_role("r1")
        assert not paths[0].exists()
        assert paths[1].exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["http://q1.qlogo.cn/g", "https://127.0.0.1/avatar", "https://q1.qlogo.cn.evil.test/avatar", "https://user:secret@q1.qlogo.cn/g"])
def test_avatar_downloader_rejects_nonplatform_urls_before_network_access(url):
    with pytest.raises(ValueError):
        asyncio.run(download_avatar(url))
