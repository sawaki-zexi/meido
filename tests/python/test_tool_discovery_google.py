import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.agent_runtime import (
    CapabilityRegistry,
    MeidoProvider,
    RuntimeManager,
    UserMessage,
)
from backend.app.agent_runtime.file_tools import ReadFileTool
from backend.app.ai import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.models import AgentRun, ModelConfiguration, RoleInput
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.mark.parametrize("remote_id", [None, "provider-stable-call"])
def test_google_discovery_with_repeated_adapter_ids_keeps_independent_audit_versions(
    tmp_path, monkeypatch, remote_id
):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    manager = RuntimeManager(sessions)
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(
        RoleInput(name="原生 Google 工具发现", profile={"profile": "设定"})
    )
    workspace = roles.workspace_path(role.id)
    (workspace / "evidence.txt").write_text("真实文件证据\n", encoding="utf-8")
    run = AgentRun(
        runId="google-discovery-without-remote-ids",
        roleId=role.id,
        sessionKey=f"role:{role.id}",
        status="created",
        startedAt=datetime.now(timezone.utc),
    )
    requests = []

    def remote_google(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {
            declaration["name"]
            for group in payload.get("tools", [])
            for declaration in group.get("functionDeclarations", [])
        }
        for group in payload.get('tools', []):
            for declaration in group.get('functionDeclarations', []):
                parameters = declaration['parameters']
                assert parameters['properties']['description']['type'] == 'string'
                assert '5-12' in parameters['properties']['description']['description']
                assert 'description' in parameters['required']
        parts = [part for item in payload["contents"] for part in item["parts"]]
        calls = [part["functionCall"] for part in parts if "functionCall" in part]
        responses = [
            part["functionResponse"] for part in parts if "functionResponse" in part
        ]
        if len(requests) == 1:
            assert "tool_search" in names and "read_file" not in names
            response_parts = [
                {
                    "functionCall": {
                        "name": "tool_search",
                        "args": {"query": "select:read_file", "description": "查找文件读取工具"},
                    },
                    "thoughtSignature": "search-signature",
                }
            ]
        elif len(requests) == 2:
            assert "read_file" in names
            assert [call["name"] for call in calls] == ["tool_search"]
            assert [response["name"] for response in responses] == ["tool_search"]
            assert json.loads(responses[0]["response"]["output"])["unlocked"] == [
                "read_file"
            ]
            response_parts = [
                {
                    "functionCall": {
                        "name": "read_file",
                        "args": {"path": "evidence.txt", "description": "读取真实文件证据"},
                    },
                    "thoughtSignature": "read-signature",
                }
            ]
        else:
            assert len(requests) == 3
            assert [call["name"] for call in calls] == ["tool_search", "read_file"]
            assert [call['args']['description'] for call in calls] == ['查找文件读取工具', '读取真实文件证据']
            assert [call["id"] for call in calls] == [remote_id or "call-0"] * 2
            previous_calls = [
                part["functionCall"]
                for item in requests[1]["contents"]
                for part in item["parts"]
                if "functionCall" in part
            ]
            assert calls[0]["id"] == previous_calls[0]["id"]
            assert [
                part["thoughtSignature"] for part in parts if "functionCall" in part
            ] == ["search-signature", "read-signature"]
            assert [response["name"] for response in responses] == [
                "tool_search",
                "read_file",
            ]
            assert "真实文件证据" in responses[1]["response"]["output"]
            response_parts = [{"text": "同次运行已读取真实文件"}]
        if remote_id is not None and "functionCall" in response_parts[0]:
            response_parts[0]["functionCall"]["id"] = remote_id
        body = {
            "candidates": [
                {"content": {"parts": response_parts}, "finishReason": "STOP"}
            ]
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content="data: " + json.dumps(body, ensure_ascii=False) + "\n\n",
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(
            timeout=timeout, transport=httpx.MockTransport(remote_google)
        ),
    )
    configuration = ModelConfiguration(
        providerId="google-test",
        provider="google-test",
        api="google-generative",
        baseUrl="https://provider.test",
        model="gemini-test",
        apiKey="isolated-test-key",
    )

    async def execute():
        capabilities = CapabilityRegistry([ReadFileTool(role.id, workspace)]).resolve(
            role_id=role.id,
            session_key=run.sessionKey,
            run_id=run.runId,
            enabled_tools={"tool_search", "read_file"},
            deferred_tool_names=frozenset({"read_file"}),
            discovery_enabled=True,
        )
        await manager.reserve_run(run, "发现后读取文件")
        events = [
            event
            async for event in manager.run(
                run,
                provider=MeidoProvider(
                    RoutedModelAdapter(ModelAdapterRegistry()), role, configuration
                ),
                model=configuration.model,
                system="",
                messages=[UserMessage("发现后读取文件")],
                capabilities=capabilities,
                max_turns=4,
            )
        ]
        assert events[-1].reason == "completed", events[-1].error
        assert events[-1].messages[-1].content == "同次运行已读取真实文件"

    asyncio.run(asyncio.wait_for(execute(), timeout=15))

    assert len(requests) == 3
    assert sessions.get_run(run.runId).status == "completed"
    audits = {item["toolName"]: item for item in sessions.list_tool_audits(run.runId)}
    assert set(audits) == {"tool_search", "read_file"}
    assert audits["tool_search"]["callId"] != audits["read_file"]["callId"]
    for name, expected_version in (("tool_search", 0), ("read_file", 1)):
        audit = audits[name]
        assert audit["resultCategory"] == "succeeded"
        revision = sessions.capability_revision_for_call(run.runId, audit["callId"])
        assert revision["visibilityVersion"] == expected_version
        assert audit["snapshotId"] == revision["snapshotId"]
    results = [
        message
        for message in sessions.list_messages(run.sessionKey)
        if message.messageType == "tool_result"
    ]
    assert len(results) == 2 and all(not message.isError for message in results)
    read = next(message for message in results if message.toolName == "read_file")
    assert read.toolCallId == audits["read_file"]["callId"]
    assert "真实文件证据" in read.content
    calls = [
        message
        for message in sessions.list_messages(run.sessionKey)
        if message.messageType == "tool_call"
    ]
    assert {message.toolCallId for message in calls} == {
        audit["callId"] for audit in audits.values()
    }
    assert all(
        message.metadata["provider_tool_call_ids"][message.toolCallId]
        == (remote_id or "call-0")
        for message in calls
    )
