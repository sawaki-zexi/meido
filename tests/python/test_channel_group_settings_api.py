"""Owner-facing group settings never need a fabricated conversation."""
import pytest

from backend.app import main
from backend.app.channels.base import ChannelAdapterError
from backend.app.channels.models import Target, TargetKind
from backend.app.models import RoleInput, RoleProfile
from test_channel_api import channel_api as channel_api


def account(channels, role_id, **changes):
    return channels.register_channel_account(role_id, "qq", account_id="group-settings", enabled=True,
                                             allow_group=True, **changes)


def endpoint(role, account_id="group-settings", chat_id=None):
    base = f"/api/roles/{role.id}/channels/accounts/{account_id}/groups"
    return base if chat_id is None else f"{base}/{chat_id}"


def test_directory_refresh_exposes_first_use_groups_without_creating_history(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    saved = account(channels, role.id)
    calls = []

    async def available(role_id, account_id):
        calls.append((role_id, account_id))
        channels.remember_targets(role_id, [Target("group-target", TargetKind.GROUP, "朋友群", account_id, "123")])
        return []

    monkeypatch.setattr(main.channel_host, "account_targets", available)
    data = client.get(endpoint(role)).json()
    assert calls == [(role.id, saved.account_id)]
    group = data["groups"][0]
    assert group["chatId"] == "123" and group["title"] == "朋友群"
    assert group["threadId"] is None
    assert not group["listening"] and not group["naturalEnabled"]
    assert channels.list_threads(role.id) == []
    assert channels.list_group_settings(role.id, saved.account_id) == []


def test_manual_group_get_is_read_only_and_settings_enable_without_normal_messages(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account(channels, role.id)
    monkeypatch.setattr(main, "_channel_connection_status", lambda _: "connected")
    base = endpoint(role, chat_id="456")
    initial = client.get(f"{base}/settings")
    assert initial.status_code == 200
    assert initial.json()["threadId"] is None and initial.json()["title"] == "456"
    assert channels.list_threads(role.id) == []
    assert channels.list_group_settings(role.id, "group-settings") == []
    assert client.put(f"{base}/natural", json={"enabled": True}).status_code == 422
    assert channels.list_threads(role.id) == []
    listening = client.put(f"{base}/listening", json={"enabled": True})
    assert listening.status_code == 200
    thread_id = listening.json()["threadId"]
    assert listening.json()["listening"] is True
    saved = client.put(f"{base}/natural", json={"enabled": True})
    assert saved.status_code == 200
    assert saved.json()["naturalEnabled"] and saved.json()["naturalEffective"]
    assert saved.json()["threadId"] == thread_id
    assert client.get(f"/api/roles/{role.id}/channels/threads?networkOnly=true").json() == {"threads": []}
    assert channels.sessions.list_messages(f"thread:{thread_id}") == []


def test_group_reply_switch_persists_without_creating_history_and_keeps_other_groups_enabled(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account(channels, role.id)
    monkeypatch.setattr(main, "_channel_connection_status", lambda _: "connected")
    base = endpoint(role, chat_id="456")
    disabled = client.put(f"{base}/replies", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["repliesEnabled"] is False
    assert disabled.json()["threadId"] is None
    assert channels.list_threads(role.id) == []
    assert client.get(endpoint(role, chat_id="789") + "/settings").json()["repliesEnabled"] is True
    from backend.app.multichannel import MultichannelStore
    restarted = MultichannelStore(channels.sessions.database_path, channels.sessions)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    assert client.get(f"{base}/settings").json()["repliesEnabled"] is False
    client.put(f"{base}/listening", json={"enabled": True})
    assert client.put(f"{base}/natural", json={"enabled": True}).status_code == 409
    assert client.put(f"{base}/replies", json={"enabled": True}).json()["repliesEnabled"] is True
    assert client.put(f"{base}/natural", json={"enabled": True}).json()["naturalEffective"] is True
    disabled = client.put(f"{base}/replies", json={"enabled": False}).json()
    assert disabled["listening"] and disabled["naturalEnabled"] and not disabled["naturalEffective"]
    assert "group_replies_disabled" in disabled["naturalBlockedReasons"]
    assert client.put(f"{base}/replies", json={"enabled": True}).json()["naturalEffective"] is True


def test_existing_group_settings_upgrade_without_changing_listening_or_reply_defaults(channel_api, monkeypatch):
    from backend.app.multichannel import MultichannelStore

    role, channels, _, client = channel_api
    account(channels, role.id)
    monkeypatch.setattr(main, "_channel_connection_status", lambda _: "connected")
    channels.set_group_listening(role.id, "group-settings", "123", True)
    with channels._connect() as db:
        db.execute("ALTER TABLE channel_group_settings DROP COLUMN replies_enabled")
        db.execute("ALTER TABLE channel_group_settings DROP COLUMN reply_revision")
    restarted = MultichannelStore(channels.sessions.database_path, channels.sessions)
    monkeypatch.setattr(main, "multichannel_store", restarted)
    state = client.get(endpoint(role, chat_id="123") + "/settings").json()
    assert state["repliesEnabled"] is True and state["listening"] is True


def test_saved_natural_setting_explains_ineffectiveness_and_can_always_be_turned_off(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account(channels, role.id)
    base = endpoint(role, chat_id="456")
    channels.set_group_listening(role.id, "group-settings", "456", True)
    thread = channels.get_thread_by_chat(role.id, "group-settings", "456", chat_type="group")
    channels.listening.set_natural_enabled(role.id, thread.thread_id, True)
    channels.register_channel_account(role.id, "qq", account_id="group-settings", enabled=False, allow_group=False)
    channels.set_channel_enabled("qq", False)
    monkeypatch.setattr(main, "_channel_connection_status", lambda _: "disconnected")
    state = client.get(f"{base}/settings").json()
    assert state["naturalEnabled"] and not state["naturalEffective"] and state["naturalPaused"]
    assert set(state["naturalBlockedReasons"]) >= {"group_response_disabled", "platform_paused", "account_paused"}
    assert "offline" not in state["naturalBlockedReasons"]  # Paused is not an unexplained connection failure.
    assert client.put(f"{base}/natural", json={"enabled": True}).status_code == 409
    disabled = client.put(f"{base}/natural", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["naturalEnabled"] is False
    assert client.put(f"{base}/listening", json={"enabled": False}).status_code == 200


def test_offline_does_not_prevent_saving_preferences_or_claim_effective_permission(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account(channels, role.id)
    monkeypatch.setattr(main, "_channel_connection_status", lambda _: "disconnected")
    base = endpoint(role, chat_id="456")
    client.put(f"{base}/listening", json={"enabled": True})
    state = client.put(f"{base}/natural", json={"enabled": True}).json()
    assert state["naturalEnabled"] is True and state["naturalEffective"] is False
    assert state["naturalBlockedReasons"] == ["offline"]


def test_unavailable_directory_preserves_cached_and_saved_groups_with_warning(channel_api, monkeypatch):
    role, channels, _, client = channel_api
    account(channels, role.id)
    channels.remember_targets(role.id, [Target("group-target", TargetKind.GROUP, "缓存群", "group-settings", "123")])
    channels.set_group_listening(role.id, "group-settings", "456", True)
    channels.ensure_channel_thread(role.id, "qq", "group-settings", "789", "group", title="已知群")

    async def unavailable(*_):
        raise ChannelAdapterError("private transport details")

    monkeypatch.setattr(main.channel_host, "account_targets", unavailable)
    result = client.get(endpoint(role))
    assert result.status_code == 200
    data = result.json()
    assert {item["chatId"] for item in data["groups"]} == {"123", "456", "789"}
    assert data["directoryWarning"] and "private transport details" not in data["directoryWarning"]


@pytest.mark.parametrize("suffix, method, body", [("", "get", None), ("/123/settings", "get", None),
    ("/123/natural", "put", {"enabled": False}), ("/123/listening", "put", {"enabled": True}),
    ("/123/replies", "put", {"enabled": False})])
def test_group_routes_reject_wrong_role_and_non_qq_accounts(channel_api, suffix, method, body):
    role, channels, _, client = channel_api
    other = main.store.create(RoleInput(name="另一角色", profile=RoleProfile(profile="隔离")))
    channels.register_channel_account(other.id, "qq", account_id="foreign", enabled=True, allow_group=True)
    channels.register_channel_account(role.id, "feishu", account_id="not-qq", enabled=True)
    for account_id in ("foreign", "not-qq", "removed"):
        response = getattr(client, method)(endpoint(role, account_id) + suffix, **({"json": body} if body else {}))
        assert response.status_code == 404
    assert channels.list_threads(role.id) == []


def test_invalid_switch_does_not_create_group(channel_api):
    role, channels, _, client = channel_api
    account(channels, role.id)
    for setting in ("listening", "natural", "replies"):
        assert client.put(endpoint(role, chat_id="123") + f"/{setting}", json={"enabled": "yes"}).status_code == 422
    assert channels.list_threads(role.id) == []


@pytest.mark.parametrize("chat_id", ["abc", "0", "00123", "１２３", "123 456", "9" * 21])
def test_manual_group_id_is_numeric_and_bounded(channel_api, chat_id):
    role, channels, _, client = channel_api
    account(channels, role.id)
    base = endpoint(role, chat_id=chat_id)
    assert client.get(f"{base}/settings").status_code == 422
    assert client.put(f"{base}/listening", json={"enabled": True}).status_code == 422
    assert client.put(f"{base}/natural", json={"enabled": False}).status_code == 422
    assert client.put(f"{base}/replies", json={"enabled": False}).status_code == 422
    assert channels.list_threads(role.id) == []


def test_group_history_directory_is_account_scoped_and_bounded(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="other", enabled=True, allow_group=True)
    channels.ensure_channel_thread(role.id, "qq", "other", "789", "group", title="已移除账号的群")
    channels.delete_account("other")
    account(channels, role.id)
    group = channels.ensure_channel_thread(role.id, "qq", "group-settings", "123", "group", title="旧群")
    channels.ensure_channel_thread(role.id, "qq", "group-settings", "456", "private", title="私聊")
    assert [item.thread_id for item in channels.list_group_threads(role.id, "group-settings")] == [group.thread_id]
    newer = channels.ensure_channel_thread(role.id, "qq", "group-settings", "999", "group", title="新群")
    assert [item.thread_id for item in channels.list_group_threads(role.id, "group-settings", limit=1)] == [newer.thread_id]
    assert channels.list_group_threads("wrong-role", "group-settings") == []


def test_legacy_thread_natural_write_obeys_same_account_permission(channel_api):
    role, channels, _, client = channel_api
    account(channels, role.id)
    channels.set_group_listening(role.id, "group-settings", "123", True)
    thread = channels.get_thread_by_chat(role.id, "group-settings", "123", chat_type="group")
    base = f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/listening/natural"
    channels.register_channel_account(role.id, "qq", account_id="group-settings", enabled=False, allow_group=False)
    assert client.put(base, json={"enabled": True}).status_code == 409
    assert client.put(base, json={"enabled": False}).status_code == 200
    channels.delete_account("group-settings")
    assert client.put(base, json={"enabled": False}).status_code == 404


def test_legacy_known_ids_do_not_break_directory_or_existing_settings(channel_api):
    role, channels, _, client = channel_api
    account(channels, role.id)
    channels.remember_targets(role.id, [Target("legacy-target", TargetKind.GROUP, "旧目录", "group-settings", "legacy-target")])
    channels.set_group_listening(role.id, "group-settings", "legacy-saved", True)
    channels.ensure_channel_thread(role.id, "qq", "group-settings", "legacy-thread", "group", title="旧会话")
    response = client.get(endpoint(role))
    assert response.status_code == 200
    assert {item["chatId"] for item in response.json()["groups"]} == {"legacy-target", "legacy-saved", "legacy-thread"}
    for chat_id in ("legacy-target", "legacy-saved", "legacy-thread"):
        base = endpoint(role, chat_id=chat_id)
        assert client.get(f"{base}/settings").status_code == 200
        assert client.put(f"{base}/listening", json={"enabled": False}).status_code == 200
    assert client.get(endpoint(role, chat_id="not-known") + "/settings").status_code == 422


def test_removed_account_preserves_read_only_thread_settings(channel_api):
    role, channels, _, client = channel_api
    account(channels, role.id)
    channels.set_group_listening(role.id, "group-settings", "123", True)
    thread = channels.get_thread_by_chat(role.id, "group-settings", "123", chat_type="group")
    channels.listening.set_natural_enabled(role.id, thread.thread_id, True)
    channels.delete_account("group-settings")
    result = client.get(f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/listening")
    assert result.status_code == 200 and result.json()["naturalEnabled"] is True
    assert result.json()["naturalEffective"] is False
    assert client.get(endpoint(role, chat_id="123") + "/settings").status_code == 404
