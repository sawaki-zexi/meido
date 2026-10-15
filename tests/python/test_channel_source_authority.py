from datetime import datetime, timezone
import asyncio

import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent,
    AssistantMessage,
    CapabilityRegistry,
    RuntimeManager,
    ToolCall,
)
from backend.app.channels.host import ChannelHost
from backend.app.channels.tools import ChannelAccountListTool
from backend.app.channels.source_authority import (
    FrozenChannelSource,
    capture_channel_source,
)
from backend.app.models import AgentRun
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


class Credentials:
    def get(self, key):
        return ""

    def set(self, key, value):
        pass


def channel_run(tmp_path, *, owner=True, chat_type="private"):
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    account = channels.register_channel_account(
        "r", "qq", account_id="a", enabled=True, allow_group=True
    )
    if owner:
        pair(channels)
    thread = channels.ensure_channel_thread(
        "r", "qq", account.account_id, "chat", chat_type
    )
    _, message = channels.append_event(
        thread,
        direction="inbound",
        content="执行工具",
        external_message_id="m",
        metadata={"senderId": "sender", "senderIsOwner": owner, "chatType": chat_type},
    )
    run = AgentRun(
        runId="run",
        roleId="r",
        sessionKey=thread.session_key,
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    sessions.create_run_with_existing_user_message(run, message.id)
    return channels, sessions, thread


def pair(channels):
    code = channels.create_pairing_code("r", "qq")
    assert channels.consume_pairing_code(
        code,
        role_id="r",
        channel="qq",
        scope="platform",
        platform_user_id="sender",
        account_id="a",
        chat_id="chat",
    )


@pytest.mark.parametrize(
    "change",
    [
        "unbind",
        "rebind",
        "disable",
        "platform",
        "block",
        "private_scope",
        "delete",
        "recreate",
    ],
)
def test_frozen_source_rejects_revoked_origin_and_rebound_owner(tmp_path, change):
    channels, sessions, thread = channel_run(tmp_path)
    source = capture_channel_source(
        channels,
        sessions,
        role_id="r",
        session_key=thread.session_key,
        run_id="run",
        owner_context=True,
    )
    assert source.authorization_denial(channels) is None
    persisted = FrozenChannelSource.from_dict(source.to_dict())
    assert persisted == source
    if change in {"unbind", "rebind"}:
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
        if change == "rebind":
            pair(channels)
    elif change == "disable":
        channels.set_account_state("a", enabled=False)
    elif change == "platform":
        channels.set_channel_enabled("qq", False)
    elif change in {"delete", "recreate"}:
        channels.delete_account("a")
        if change == "recreate":
            channels.register_channel_account("r", "qq", account_id="a", enabled=True)
    else:
        channels.register_channel_account(
            "r",
            "qq",
            account_id="a",
            enabled=True,
            **(
                {"blocked_senders": ["sender"]}
                if change == "block"
                else {"allow_private": False}
            ),
        )
    assert persisted.authorization_denial(channels)


def test_old_owner_message_cannot_capture_a_new_binding_generation(tmp_path):
    channels, sessions, thread = channel_run(tmp_path)
    channels.unbind_identity(channels.list_identities()[0]["identityId"])
    pair(channels)
    with pytest.raises(PermissionError):
        capture_channel_source(
            channels,
            sessions,
            role_id="r",
            session_key=thread.session_key,
            run_id="run",
            owner_context=True,
        )


def test_unpaired_original_sender_cannot_be_upgraded_by_model_or_later_pairing(
    tmp_path,
):
    channels, sessions, thread = channel_run(tmp_path, owner=False, chat_type="group")
    source = capture_channel_source(
        channels,
        sessions,
        role_id="r",
        session_key=thread.session_key,
        run_id="run",
        owner_context=False,
    )
    pair(channels)
    assert not source.owner_context
    assert source.authorization_denial(channels) is None
    with pytest.raises(PermissionError):
        capture_channel_source(
            channels,
            sessions,
            role_id="r",
            session_key=thread.session_key,
            run_id="run",
            owner_context=True,
        )


def test_source_capture_rejects_run_from_another_thread(tmp_path):
    channels, sessions, _ = channel_run(tmp_path)
    other = channels.ensure_channel_thread("r", "qq", "a", "other", "private")
    with pytest.raises(PermissionError):
        capture_channel_source(
            channels,
            sessions,
            role_id="r",
            session_key=other.session_key,
            run_id="run",
            owner_context=True,
        )


def test_rebinding_between_provider_requests_revokes_search_and_loaded_execution(
    tmp_path,
):
    channels, sessions, thread = channel_run(tmp_path)
    source = capture_channel_source(
        channels,
        sessions,
        role_id="r",
        session_key=thread.session_key,
        run_id="run",
        owner_context=True,
    )
    capabilities = CapabilityRegistry(
        [ChannelAccountListTool(ChannelHost(channels))]
    ).resolve(
        role_id="r",
        session_key=thread.session_key,
        run_id="run",
        enabled_tools={"tool_search", "account_list"},
        discovery_enabled=True,
        deferred_tool_names=frozenset({"account_list"}),
        authorize_tool=lambda definition, context: (
            source.authorization_denial(channels) or True
        ),
    )

    class Provider:
        count = 0

        async def stream_response(self, *, tools, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                "load", "tool_search", {"query": "select:account_list"}
                            ),
                        )
                    )
                )
            elif self.count == 2:
                assert "account_list" in {item["function"]["name"] for item in tools}
                channels.unbind_identity(channels.list_identities()[0]["identityId"])
                pair(channels)
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall("old-owner-call", "account_list", {}),
                            ToolCall(
                                "old-owner-search",
                                "tool_search",
                                {"query": "select:account_list"},
                            ),
                        )
                    )
                )
            else:
                assert all(
                    message.is_error and "revoked" in message.content
                    for message in messages[-2:]
                )
                yield AssistantDoneEvent(AssistantMessage("denied"))

    async def execute():
        manager = RuntimeManager(sessions)
        events = [
            event
            async for event in manager.run(
                sessions.get_run("run"),
                provider=Provider(),
                model="",
                system="",
                messages=[],
                capabilities=capabilities,
            )
        ]
        assert events[-1].reason == "completed"

    asyncio.run(execute())
    audits = {item["callId"]: item for item in sessions.list_tool_audits("run")}
    assert (
        audits["old-owner-call"]["resultCategory"]
        == audits["old-owner-search"]["resultCategory"]
        == "denied"
    )
    assert len(sessions.get_run("run").modelSnapshot["capabilityRevisions"]) == 2
