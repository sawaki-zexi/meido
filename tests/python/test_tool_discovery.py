import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent,
    AssistantMessage,
    CapabilityRegistry,
    RuntimeManager,
    ToolCall,
    ToolDefinition,
    AgentToolResult,
    ToolContext,
    CancellationToken,
)
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class ArchiveTool:
    definition = ToolDefinition(
        "archive.read",
        "读取归档笔记 archive notes",
        {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        exposure="deferred",
        source="plugin:archive",
        external_allowed=True,
    )

    async def execute(self, arguments, context, on_update=None):
        return AgentToolResult("archive evidence: " + arguments["topic"])


def test_discovery_is_durable_and_authorizes_subsequent_calls_in_same_reply(
    tmp_path,
):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = RuntimeManager(sessions)
    run = AgentRun(
        runId="discover-run",
        roleId="r",
        sessionKey="role:r",
        status="created",
        startedAt=datetime.now(timezone.utc),
    )

    class Provider:
        requests = 0

        async def stream_response(self, *, tools, messages, **kwargs):
            self.requests += 1
            schemas = {
                item["function"]["name"]: item["function"]["parameters"]
                for item in tools
            }
            if self.requests == 1:
                assert set(schemas) == {"tool_search"}
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                "search",
                                "tool_search",
                                {"query": "select:archive.read"},
                            ),
                            ToolCall(
                                "premature",
                                "archive.read",
                                {"topic": "forged same reply"},
                            ),
                        )
                    )
                )
            elif self.requests == 2:
                assert schemas["archive.read"]["required"] == ["topic", "description"]
                persisted = sessions.list_messages("role:r")
                found = next(
                    item
                    for item in persisted
                    if item.toolCallId == "search" and item.messageType == "tool_result"
                )
                assert json.loads(found.content)["unlocked"] == ["archive.read"]
                rejected = next(
                    item
                    for item in messages
                    if getattr(item, "tool_call_id", None) == "premature"
                )
                assert not rejected.is_error and rejected.content == "archive evidence: forged same reply"
                assert "capabilityRevisions" not in sessions.get_run(run.runId).modelSnapshot
                assert sessions.call_binding(run.runId, "search")["active"] is True
                assert sessions.call_binding(run.runId, "premature")["active"] is True
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall(
                                "read",
                                "archive.read",
                                {"topic": "accepted next request"},
                            ),
                        )
                    )
                )
            else:
                assert messages[-1].content == "archive evidence: accepted next request"
                yield AssistantDoneEvent(AssistantMessage(content="done"))

    async def execute():
        capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools={"tool_search", "archive.read"},
            discovery_enabled=True,
        )
        initial = capabilities.snapshot.to_dict()
        await manager.reserve_run(run, "发现并读取归档")
        events = [
            event
            async for event in manager.run(
                run,
                provider=Provider(),
                model="",
                system="",
                messages=[],
                capabilities=capabilities,
                max_turns=4,
            )
        ]
        assert events[-1].reason == "completed", events[-1].error
        assert capabilities.snapshot.to_dict() == initial
        assert [item["name"] for item in initial["tools"]] == ["tool_search"]
        assert (
            sessions.capability_revision_for_call(run.runId, "premature")[
                "visibilityVersion"
            ]
            == 1
        )
        assert (
            sessions.capability_revision_for_call(run.runId, "read")[
                "visibilityVersion"
            ]
            == 1
        )
        audits = {item["callId"]: item for item in sessions.list_tool_audits(run.runId)}
        assert (
            audits["search"]["snapshotId"] == initial["snapshotId"]
        )
        assert (
            audits["read"]["snapshotId"] == capabilities.current().snapshot.snapshot_id
        )
        assert audits["premature"]["resultCategory"] == "succeeded"
        assert all(item["endedAt"] for item in audits.values())

    asyncio.run(execute())


def test_keyword_and_exact_search_respect_frozen_admission_risk_and_external_scope():
    class OtherTool(ArchiveTool):
        def __init__(self, definition):
            self.definition = definition

    tools = [
        ArchiveTool(),
        OtherTool(
            replace(
                ArchiveTool.definition,
                name="archive.write",
                description="写入归档 archive notes",
                risk="mutating",
            )
        ),
        OtherTool(
            replace(ArchiveTool.definition, name="private.read", external_allowed=False)
        ),
        OtherTool(replace(ArchiveTool.definition, name="disabled.read")),
        OtherTool(
            replace(ArchiveTool.definition, name="hidden.read", exposure="hidden")
        ),
    ]
    registry = CapabilityRegistry(tools)
    admitted = {
        "tool_search",
        "archive.read",
        "archive.write",
        "private.read",
        "hidden.read",
    }
    capabilities = registry.resolve(
        role_id="r",
        session_key="thread:g",
        run_id="run",
        enabled_tools=admitted,
        external_only=True,
        discovery_enabled=True,
    )
    # Later grants and registrations cannot enlarge this run's admitted catalog.
    admitted.add("disabled.read")
    registry.register(OtherTool(replace(ArchiveTool.definition, name="new.read")))
    context = ToolContext("r", "thread:g", "run", CancellationToken(), "search")

    async def search(arguments):
        return json.loads(
            (await capabilities.get("tool_search").execute(arguments, context)).content
        )

    chinese = asyncio.run(
        search({"query": "归档", "top_k": 1, "allowed_risk": ["read-only"]})
    )
    assert [item["name"] for item in chinese["matched"]] == ["archive.read"]
    assert chinese["unlocked"] == ["archive.read"]
    english = asyncio.run(
        search({"query": "archive notes", "top_k": 2, "allowed_risk": ["write"]})
    )
    assert english["unlocked"] == ["archive.write"]
    precise = asyncio.run(
        search(
            {
                "query": "select:private.read,disabled.read,hidden.read,new.read,archive.read",
                "allowed_risk": ["write"],
            }
        )
    )
    assert precise["matched"] == precise["unlocked"] == []
    assert precise["tip"]
    assert asyncio.run(search({"query": "不存在的功能"}))["unlocked"] == []
    assert asyncio.run(search({"query": "select:tool_search"}))["already_loaded"] == [
        "tool_search"
    ]


def test_live_revocation_blocks_execution_without_mutating_issued_schema_versions(
    tmp_path,
):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = RuntimeManager(sessions)
    run = AgentRun(
        runId="revoke-run",
        roleId="r",
        sessionKey="role:r",
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    allowed = {"archive.read", "tool_search"}

    class Provider:
        count = 0

        async def stream_response(self, *, tools, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(ToolCall("load", "tool_search", {"query": "归档"}),)
                    )
                )
            elif self.count == 2:
                assert "archive.read" in {item["function"]["name"] for item in tools}
                allowed.remove("archive.read")
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(
                            ToolCall("revoked", "archive.read", {"topic": "secret"}),
                        )
                    )
                )
            else:
                assert messages[-1].is_error and "revoked" in messages[-1].content
                yield AssistantDoneEvent(AssistantMessage(content="permission revoked"))

    async def execute():
        capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools=set(allowed),
            discovery_enabled=True,
            authorize_tool=lambda definition, context: definition.name in allowed,
        )
        await manager.reserve_run(run, "读取归档")

        async def persist(event):
            return sessions.append_agent_message(
                "role:r", event.message, run_id=run.runId
            )[0]

        events = [
            event
            async for event in manager.run(
                run,
                provider=Provider(),
                model="",
                system="",
                messages=[],
                capabilities=capabilities,
                max_turns=4,
                on_message_end=persist,
            )
        ]
        assert events[-1].reason == "completed", events[-1].error
        assert "capabilityRevisions" not in sessions.get_run(run.runId).modelSnapshot
        assert sessions.call_binding(run.runId, "revoked")["visibilityVersion"] == 1

    asyncio.run(execute())


def test_entered_tool_finishes_naturally_after_registry_permission_changes(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    effect = tmp_path / "effect.txt"

    async def execute():
        ready, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        allowed = True

        class DelayedWrite:
            definition = ToolDefinition(
                "delayed.write", "Write a local effect", risk="mutating"
            )

            async def execute(self, arguments, context, on_update=None):
                ready.set()
                try:
                    await release.wait()
                    effect.write_text("permission was revoked", encoding="utf-8")
                    return AgentToolResult("written")
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        class Provider:
            count = 0

            async def stream_response(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    yield AssistantDoneEvent(
                        AssistantMessage(
                            tool_calls=(ToolCall("write", "delayed.write", {}),)
                        )
                    )
                else:
                    yield AssistantDoneEvent(AssistantMessage("done"))

        run = AgentRun(
            runId="running-revocation",
            roleId="r",
            sessionKey="role:r",
            status="created",
            startedAt=datetime.now(timezone.utc),
        )
        manager = RuntimeManager(sessions)
        capabilities = CapabilityRegistry([DelayedWrite()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools={"delayed.write", "tool_search"},
            discovery_enabled=True,
            authorize_tool=lambda definition, context: allowed,
        )
        await manager.reserve_run(run, "执行期间撤权")

        async def consume():
            return [
                event
                async for event in manager.run(
                    run,
                    provider=Provider(),
                    model="",
                    system="",
                    messages=[],
                    capabilities=capabilities,
                )
            ]

        running = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            allowed = False
            try:
                await asyncio.wait_for(cancelled.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            release.set()
            await asyncio.wait_for(running, 1)
            assert effect.read_text(encoding="utf-8") == "permission was revoked"
            assert not cancelled.is_set()
            assert sessions.list_tool_audits(run.runId)[0]["resultCategory"] == "succeeded"
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(execute())


def test_repeated_parent_cancellation_waits_for_real_tool_resource_cleanup(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")

    async def execute():
        ready, cleanup_ready, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        cleanup_finished, cleanup_interrupted = asyncio.Event(), asyncio.Event()
        handles = []
        allowed = True

        class HeldResource:
            definition = ToolDefinition(
                "held.resource", "Hold a real file until cleanup completes"
            )

            async def execute(self, arguments, context, on_update=None):
                handle = (tmp_path / "held.txt").open("w", encoding="utf-8")
                handles.append(handle)
                ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_ready.set()
                    try:
                        await release.wait()
                        handle.close()
                        cleanup_finished.set()
                    except asyncio.CancelledError:
                        cleanup_interrupted.set()
                        raise

        class Provider:
            requests = 0

            async def stream_response(self, **kwargs):
                self.requests += 1
                assert self.requests == 1, (
                    "cancelled cleanup must not resume the provider"
                )
                yield AssistantDoneEvent(
                    AssistantMessage(
                        tool_calls=(ToolCall("hold", "held.resource", {}),)
                    )
                )

        manager = RuntimeManager(sessions)
        run = AgentRun(
            runId="repeat-cancel-cleanup",
            roleId="r",
            sessionKey="role:r",
            status="created",
            startedAt=datetime.now(timezone.utc),
        )
        capabilities = CapabilityRegistry([HeldResource()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools={"held.resource", "tool_search"},
            discovery_enabled=True,
            authorize_tool=lambda definition, context: allowed,
        )
        await manager.reserve_run(run, "重复取消必须等待真实资源清理")

        async def consume():
            return [
                event
                async for event in manager.run(
                    run,
                    provider=Provider(),
                    model="",
                    system="",
                    messages=[],
                    capabilities=capabilities,
                )
            ]

        running = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert manager.cancel_run(run.runId)
            await asyncio.wait_for(cleanup_ready.wait(), 1)
            for _ in range(6):
                for _ in range(4):
                    await asyncio.sleep(0)
                running.cancel()
            for _ in range(4):
                await asyncio.sleep(0)
            assert not cleanup_interrupted.is_set(), (
                "parent cancellation reached the resource finalizer"
            )
            assert not running.done(), "run finished before its real file was closed"
            assert sessions.get_run(run.runId).status == "running"
            assert manager.lock_for("r").locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 1)
            assert cleanup_finished.is_set() and handles[0].closed
            assert sessions.get_run(run.runId).status == "cancelled"
            assert not manager.lock_for("r").locked()
            assert sessions.list_tool_audits(run.runId)[0]["endedAt"] is not None
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            for handle in handles:
                handle.close()

    asyncio.run(execute())


def test_window_restoration_uses_current_definition_and_current_permission():
    from backend.app.agent_runtime.types import ToolResultMessage
    history = [ToolResultMessage('search', 'tool_search', '{}', unlocked_tools=('archive.read',))]
    tool = ArchiveTool()
    tool.definition = replace(tool.definition, input_schema={'type': 'object', 'properties': {'other': {'type': 'string'}}})
    restored = CapabilityRegistry([tool]).resolve(role_id='r', session_key='role:r', run_id='new', discovery_enabled=True).select_window(history)
    assert restored.definition('archive.read').input_schema == tool.definition.input_schema
    revoked = CapabilityRegistry([tool]).resolve(role_id='r', session_key='role:r', run_id='denied', discovery_enabled=True,
        enabled_tools={'tool_search'}).select_window(history)
    assert [item['function']['name'] for item in revoked.provider_schemas()] == ['tool_search']


def test_service_revision_inherits_frozen_grant_and_keeps_old_catalog_immutable():
    from backend.app.agent_runtime.discovery import ServiceGrant

    grant = ServiceGrant("owner-grant", "notes", "r", frozenset({"read_only"}))
    capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
        role_id="r",
        session_key="role:r",
        run_id="mount",
        enabled_tools={"archive.read", "tool_search"},
        discovery_enabled=True,
        service_grants=(grant,),
    )
    initial = capabilities.snapshot.to_dict()
    context = ToolContext(
        "r", "role:r", "mount", CancellationToken(), "mount-call", initial["snapshotId"]
    )
    remote = ArchiveTool()
    remote.definition = replace(
        remote.definition, name="notes.read", source="mcp:notes", exposure="direct"
    )
    with pytest.raises(PermissionError):
        capabilities.stage_service_tools(
            context,
            grant_id="untrusted",
            parent_snapshot_id=initial["snapshotId"],
            tools=(remote,),
        )
    with pytest.raises(ValueError):
        capabilities.stage_service_tools(
            context,
            grant_id=grant.grant_id,
            parent_snapshot_id="stale",
            tools=(remote,),
        )
    unsafe = ArchiveTool()
    unsafe.definition = replace(remote.definition, risk="mutating")
    with pytest.raises(PermissionError):
        capabilities.stage_service_tools(
            context,
            grant_id=grant.grant_id,
            parent_snapshot_id=initial["snapshotId"],
            tools=(unsafe,),
        )
    assert capabilities.stage_service_tools(
        context,
        grant_id=grant.grant_id,
        parent_snapshot_id=initial["snapshotId"],
        tools=(remote,),
    ) == ("notes.read",)
    revision = capabilities.pending_revision("mount-call")
    assert "notes.read" not in {
        item["function"]["name"] for item in capabilities.current().provider_schemas()
    }
    capabilities.publish_revision(revision)
    assert capabilities.snapshot.to_dict() == initial
    assert (
        revision.snapshot.candidate_catalog_id
        != capabilities.snapshot.candidate_catalog_id
    )
    assert (
        next(
            item for item in revision.snapshot.candidates if item.name == "notes.read"
        ).exposure
        == "deferred"
    )
    search_context = replace(
        context,
        tool_call_id="discover-remote",
        capability_snapshot_id=revision.snapshot.snapshot_id,
    )
    found = asyncio.run(
        revision.get("tool_search").execute(
            {"query": "select:notes.read"}, search_context
        )
    )
    assert json.loads(found.content)["unlocked"] == ["notes.read"]
    loaded = capabilities.pending_revision("discover-remote")
    capabilities.publish_revision(loaded)
    result = asyncio.run(
        loaded.get("notes.read").execute({"topic": "remote evidence"}, search_context)
    )
    assert result.content == "archive evidence: remote evidence"


def test_cancelling_after_discovery_persistence_does_not_activate_or_send_another_request(
    tmp_path,
):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = RuntimeManager(sessions)
    run = AgentRun(
        runId="cancel-discovery",
        roleId="r",
        sessionKey="role:r",
        status="created",
        startedAt=datetime.now(timezone.utc),
    )

    class Provider:
        requests = 0

        async def stream_response(self, **kwargs):
            self.requests += 1
            assert self.requests == 1
            yield AssistantDoneEvent(
                AssistantMessage(
                    tool_calls=(ToolCall("search", "tool_search", {"query": "归档"}),)
                )
            )

    async def execute():
        capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools={"archive.read", "tool_search"},
            discovery_enabled=True,
        )
        await manager.reserve_run(run, "发现后停止")

        async def persist(event):
            message = sessions.append_agent_message(
                "role:r", event.message, run_id=run.runId
            )[0]
            if getattr(event.message, "tool_name", "") == "tool_search":
                manager.cancel_run(run.runId, interrupt=False)
            return message

        events = [
            event
            async for event in manager.run(
                run,
                provider=Provider(),
                model="",
                system="",
                messages=[],
                capabilities=capabilities,
                max_turns=3,
                on_message_end=persist,
            )
        ]
        assert events[-1].reason == "cancelled"
        assert capabilities.current().snapshot.visibility_version == 0
        assert not capabilities._discovery.pending
        assert "capabilityRevisions" not in sessions.get_run(run.runId).modelSnapshot
        assert "toolSelection" not in sessions.get_run(run.runId).modelSnapshot

    asyncio.run(execute())


def test_failed_message_persistence_never_publishes_the_staged_version(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = RuntimeManager(sessions)
    run = AgentRun(
        runId="failed-persistence",
        roleId="r",
        sessionKey="role:r",
        status="created",
        startedAt=datetime.now(timezone.utc),
    )

    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(
                AssistantMessage(
                    tool_calls=(ToolCall("load", "tool_search", {"query": "归档"}),)
                )
            )

    async def execute():
        capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
            role_id="r",
            session_key="role:r",
            run_id=run.runId,
            enabled_tools={"archive.read", "tool_search"},
            discovery_enabled=True,
        )
        await manager.reserve_run(run, "discover")

        async def persist(event):
            if getattr(event.message, "tool_name", "") == "tool_search":
                raise OSError("controlled storage failure")
            return sessions.append_agent_message(
                "role:r", event.message, run_id=run.runId
            )[0]

        with pytest.raises(OSError, match="storage failure"):
            async for _ in manager.run(
                run,
                provider=Provider(),
                model="",
                system="",
                messages=[],
                capabilities=capabilities,
                on_message_end=persist,
            ):
                pass
        assert capabilities.current().snapshot.visibility_version == 0
        assert not capabilities._discovery.pending
        assert sessions.get_run(run.runId).status == "failed"
        assert "capabilityRevisions" not in sessions.get_run(run.runId).modelSnapshot
        assert "toolSelection" not in sessions.get_run(run.runId).modelSnapshot

    asyncio.run(execute())


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "archive", "top_k": True},
        {"query": "archive", "top_k": 0},
        {"query": "archive", "top_k": 11},
        {"query": "archive", "allowed_risk": ["mutating"]},
        {"query": "archive", "role_id": "other"},
        {"query": "archive", "session_key": "thread:other"},
        {"query": "archive", "visible_tool_names": []},
        {"query": "archive", "external_tools_only": False},
        {"query": "archive", "capability_snapshot_id": "forged"},
    ],
)
def test_discovery_rejects_malformed_filters_and_model_context_overrides(arguments):
    capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
        role_id="r",
        session_key="role:r",
        run_id="run",
        enabled_tools={"archive.read", "tool_search"},
        discovery_enabled=True,
    )
    with pytest.raises(ValueError):
        asyncio.run(
            capabilities.get("tool_search").execute(
                arguments, ToolContext("r", "role:r", "run", CancellationToken())
            )
        )


@pytest.mark.parametrize(
    "role_id,session_key,run_id,version",
    [
        ("other", "role:r", "run", None),
        ("r", "thread:other", "run", None),
        ("r", "role:r", "foreign-run", None),
    ],
)
def test_discovery_requires_host_bound_role_origin_and_run(
    role_id, session_key, run_id, version
):
    capabilities = CapabilityRegistry([ArchiveTool()]).resolve(
        role_id="r",
        session_key="role:r",
        run_id="run",
        enabled_tools={"archive.read", "tool_search"},
        discovery_enabled=True,
    )
    with pytest.raises(PermissionError):
        asyncio.run(
            capabilities.get("tool_search").execute(
                {"query": "archive"},
                ToolContext(
                    role_id, session_key, run_id, CancellationToken(), "search", version
                ),
            )
        )


def test_discovery_keeps_result_json_bounded_before_staging_activation():
    tools = []
    for index in range(30):
        tool = ArchiveTool()
        tool.definition = replace(
            tool.definition, name=f"notes.{index}", description="\0" * 160
        )
        tools.append(tool)
    capabilities = CapabilityRegistry(tools).resolve(
        role_id="r",
        session_key="role:r",
        run_id="run",
        enabled_tools={"tool_search", *(tool.definition.name for tool in tools)},
        discovery_enabled=True,
    )
    search = capabilities.get("tool_search")
    result = asyncio.run(
        search.execute(
            {"query": "select:" + ",".join(tool.definition.name for tool in tools)},
            ToolContext("r", "role:r", "run", CancellationToken(), "load"),
        )
    )
    assert len(result.content) <= search.definition.output_limit
    payload = json.loads(result.content)
    # Whole declarations include the host call-intent schema in their byte cost.
    assert len(payload["unlocked"]) == 23
    assert payload["skipped_due_to_budget"] == [f"notes.{index}" for index in range(23, 30)]
    assert payload["budget"]["new_bytes"] <= 32768
    assert search.discovery.pending["load"] == tuple(payload["unlocked"])
    assert payload["truncated"]
