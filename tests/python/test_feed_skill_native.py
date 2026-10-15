"""Opt-in public HTTP checks against the real, separately installed Feed MCP.

MEIDO_FEED_NATIVE_TESTS=1 requires MEIDO_FEED_SOURCE and MEIDO_FEED_PYTHON.
Missing dependencies fail explicitly once enabled; no packages are installed here.
"""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import httpx
import pytest

from backend.app import main
from backend.app.ai.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.mcp_config import McpConfigStore, minimal_environment
from backend.app.mcp_runtime import McpHost
from test_feed_skill import isolated_owner_components
from test_mcp_http import setup_host


pytestmark = pytest.mark.skipif(os.environ.get("MEIDO_FEED_NATIVE_TESTS") != "1",
    reason="real Feed checks require MEIDO_FEED_NATIVE_TESTS=1")
FEED_SHA = "210a79512c5cec008dd24c70f7c9c4e2616f5034"


@pytest.fixture
def installed_feed(tmp_path):
    paths = {}
    for key in ("MEIDO_FEED_SOURCE", "MEIDO_FEED_PYTHON"):
        configured = os.environ.get(key)
        if not configured:
            pytest.fail(f"native Feed checks enabled, but {key} is missing")
        paths[key] = Path(configured).expanduser().resolve()
    source, executable = paths["MEIDO_FEED_SOURCE"], paths["MEIDO_FEED_PYTHON"]
    repository = Path(__file__).resolve().parents[2]
    if (source.is_relative_to(repository) or tmp_path.resolve().is_relative_to(repository)
            or not (source / "run_mcp.py").is_file() or not executable.is_file()):
        pytest.fail("Feed source and test directory must be outside Meido; Python must be an existing isolated executable")
    try:
        actual_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, timeout=10).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, text=True, timeout=10)
        versions = json.loads(subprocess.check_output([str(executable), "-I", "-c",
            "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in ['mcp','requests']}))"],
            env=minimal_environment(), text=True, timeout=10))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        pytest.fail(f"native Feed dependency preflight failed: {error}")
    assert actual_sha == FEED_SHA, "native dependency source must match the audited SHA"
    assert not dirty, "tracked upstream source changed after audit"
    assert versions == {"mcp": "1.30.0", "requests": "2.34.2"}, versions
    # Copy only audited runtime files into an external temporary test directory;
    # no upstream implementation is vendored into Meido.
    service = tmp_path / "feed-service"
    service.mkdir()
    shutil.copy2(source / "run_mcp.py", service / "run_mcp.py")
    shutil.copytree(source / "src", service / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (service / "feed_mcp.json").write_text(json.dumps({
        "db_path": (tmp_path / "feed-data.sqlite3").as_posix(), "poll_ttl_seconds": 60,
    }), encoding="utf-8")
    return executable, service, versions


@contextmanager
def local_feeds():
    timestamp = datetime.now(UTC)
    state = {"fail": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append({"path": self.path, "failed": state["fail"]})
            if state["fail"] or self.path not in ("/rss", "/atom"):
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/rss":
                entries = "".join(f"<item><title>RSS中文独有{index}</title><description>本地验收甲{index}</description>"
                    f"<link>{base}/article/rss-{index}</link><pubDate>{format_datetime(timestamp-timedelta(minutes=index), usegmt=True)}</pubDate></item>"
                    for index in (1, 2))
                body = f'<rss version="2.0"><channel><title>RSS fixture</title>{entries}</channel></rss>'
                content_type = "application/rss+xml; charset=utf-8"
            else:
                entries = "".join(f"<entry><id>urn:meido:atom:{index}</id><title>Atom中文独有{index}</title>"
                    f'<link rel="alternate" href="{base}/article/atom-{index}"/><summary>本地验收乙{index}</summary>'
                    f"<updated>{(timestamp-timedelta(minutes=index+2)).isoformat()}</updated></entry>" for index in (1, 2))
                body = f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom fixture</title>{entries}</feed>'
                content_type = "application/atom+xml; charset=utf-8"
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "local Feed fixture did not stop"


def provider_tool(messages, call_id):
    content = next(item["content"] for item in reversed(messages)
        if item["role"] == "tool" and item.get("tool_call_id") == call_id)
    assert isinstance(content, str)
    assert not content.startswith(("[denied]", "[failed]", "[timed_out]")), content
    return content


def catalog_payload(content):
    # A real structured MCP result may follow the text JSON; decode the first
    # JSON value rather than assuming it is the entire provider message.
    return json.JSONDecoder().raw_decode(content)[0]


def assert_feed_selected(sessions, role_id):
    run = sessions.get_run(sessions.list_messages(f"role:{role_id}")[-1].runId)
    assert [(item["id"], item["source"]) for item in run.modelSnapshot["capabilities"]["skills"]] == [("feed-manage", "builtin")]
    return run


def test_real_feed_http_seven_actions_dynamic_schemas_fresh_list_and_restart(tmp_path, monkeypatch, installed_feed):
    executable, service, versions = installed_feed
    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    home, temp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    temp.mkdir()
    saved = client.post("/api/mcp/servers", json={
        "serverId": "reading_room", "name": "我的本机阅读源", "command": [str(executable), str(service / "run_mcp.py")],
        "cwd": str(service), "roleIds": [role["id"]], "risk": "external", "timeoutSeconds": 15,
        "env": {"HOME": str(home), "USERPROFILE": str(home), "TMP": str(temp), "TEMP": str(temp),
                "NO_PROXY": "127.0.0.1,localhost"},
    })
    assert saved.status_code == 201, saved.text
    server_id = saved.json()["server"]["serverId"]
    observations = []
    manage = query = ""
    with local_feeds() as (base, state):
        class Adapter:
            turn = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                nonlocal manage, query
                self.turn += 1
                names = {item["function"]["name"] for item in tools}
                assert "Feed 订阅管理与查询" in json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
                observations.append({"phase": "seven-actions", "turn": self.turn, "tools": sorted(names), "messages": messages})
                if self.turn == 1:
                    assert not any(name.startswith("mcp_reading_room__") for name in names)
                    yield ModelToolCall("load-management", "tool_search", {"query": "select:mcp_list,mcp_add"})
                elif self.turn == 2:
                    yield ModelToolCall("list-services", "mcp_list", {})
                elif self.turn == 3:
                    available = json.loads(provider_tool(messages, "list-services"))["servers"]
                    assert [(item["serverId"], item["name"]) for item in available] == [(server_id, "我的本机阅读源")]
                    yield ModelToolCall("connect-feed", "mcp_add", {"server_id": available[0]["serverId"]})
                elif self.turn == 4:
                    actual_names = json.loads(provider_tool(messages, "connect-feed"))["tools"]
                    manage = next(name for name in actual_names if name.endswith("__feed_manage"))
                    query = next(name for name in actual_names if name.endswith("__feed_query"))
                    assert manage not in names and query not in names
                    yield ModelToolCall("load-remote", "tool_search", {"query": f"select:{manage},{query}"})
                elif self.turn == 5:
                    assert manage in names and query in names
                    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in tools}
                    assert "action" in schemas[manage]["properties"] and "url" in schemas[manage]["properties"]
                    assert "page_size" in schemas[query]["properties"]
                    yield ModelToolCall("initial-list", manage, {"action": "list"})
                    yield ModelToolCall("subscribe-rss", manage, {"action": "subscribe", "name": "本机RSS唯一源", "url": base+"/rss"})
                    yield ModelToolCall("subscribe-atom", manage, {"action": "subscribe", "name": "本机Atom唯一源", "url": base+"/atom"})
                    yield ModelToolCall("fresh-list", manage, {"action": "list"})
                elif self.turn == 6:
                    listed = provider_tool(messages, "fresh-list")
                    assert base+"/rss" in listed and base+"/atom" in listed
                    assert "没有订阅" in provider_tool(messages, "initial-list")
                    yield ModelToolCall("latest", query, {"action": "latest", "limit": 10})
                    yield ModelToolCall("summary", query, {"action": "summary"})
                    yield ModelToolCall("search", query, {"action": "search", "keyword": "RSS中文独有1"})
                    yield ModelToolCall("catalog-1", query, {"action": "catalog", "page": 1, "page_size": 2})
                    yield ModelToolCall("catalog-2", query, {"action": "catalog", "page": 2, "page_size": 2})
                elif self.turn == 7:
                    latest = provider_tool(messages, "latest")
                    assert all(value in latest for value in ("RSS中文独有1", "Atom中文独有1", base+"/article/rss-1", base+"/article/atom-1"))
                    assert "sources=2 items=4" in provider_tool(messages, "summary")
                    search = provider_tool(messages, "search")
                    assert "RSS中文独有1" in search and "Atom中文" not in search and "RSS中文独有2" not in search
                    pages = [catalog_payload(provider_tool(messages, f"catalog-{page}")) for page in (1, 2)]
                    assert pages[0]["total"] == 4 and pages[0]["next_page"] == 2 and pages[0]["has_more"]
                    assert pages[1]["total"] == 4 and not pages[1]["has_more"]
                    assert len({item["url"] for page in pages for item in page["items"]}) == 4
                    yield ModelToolCall("unsubscribe", manage, {"action": "unsubscribe", "name": "本机RSS唯一源"})
                    yield ModelToolCall("after-remove-list", manage, {"action": "list"})
                    yield ModelToolCall("after-remove-query", query, {"action": "latest", "limit": 10})
                else:
                    assert self.turn == 8
                    assert "已取消订阅" in provider_tool(messages, "unsubscribe")
                    remaining = provider_tool(messages, "after-remove-list")
                    assert "本机Atom唯一源" in remaining and "本机RSS唯一源" not in remaining
                    content = provider_tool(messages, "after-remove-query")
                    assert "Atom中文独有1" in content and "RSS中文" not in content
                    yield ModelTextDelta("FEED_HTTP_SEVEN_VERIFIED 服务返回了RSS与Atom文章，现仅订阅本机Atom唯一源。")

        monkeypatch.setattr(main, "model_adapter", Adapter())
        try:
            response = client.post(f"/api/roles/{role['id']}/messages", json={"content":
                f"$feed-manage 在我的本机阅读源中订阅本机RSS唯一源 {base}/rss 和本机Atom唯一源 {base}/atom，"
                "查询最新、概况、搜索RSS中文独有1及两页文章目录，再取消本机RSS唯一源并核对。"})
            assert "FEED_HTTP_SEVEN_VERIFIED" in response.text, response.text
            assert_feed_selected(sessions, role["id"])
            # Reopen the host/store at the same public configuration boundary.
            # The previous HTTP run has already released its real stdio process.
            monkeypatch.setattr(main, "mcp_host", McpHost(McpConfigStore(configs.path, configs.credentials)))

            class RestartAdapter:
                turn = 0

                async def stream_structured_messages(self, messages, configuration, tools):
                    self.turn += 1
                    observations.append({"phase": "restart-fresh-list", "turn": self.turn, "messages": messages})
                    assert manage in {item["function"]["name"] for item in tools}
                    if self.turn == 1:
                        yield ModelToolCall("restart-fresh-list", manage, {"action": "list"})
                        yield ModelToolCall("restart-query", query, {"action": "latest", "limit": 10})
                    elif self.turn == 2:
                        listed = provider_tool(messages, "restart-fresh-list")
                        assert "本机Atom唯一源" in listed and "本机RSS唯一源" not in listed
                        assert "Atom中文独有1" in provider_tool(messages, "restart-query")
                        yield ModelToolCall("last-unsubscribe", manage, {"action": "unsubscribe", "name": "本机Atom唯一源"})
                        yield ModelToolCall("last-list", manage, {"action": "list"})
                    else:
                        assert "没有订阅" in provider_tool(messages, "last-list")
                        yield ModelTextDelta("FEED_HTTP_RESTART_VERIFIED 本轮已重新查询来源并清空本机测试订阅。")

            monkeypatch.setattr(main, "model_adapter", RestartAdapter())
            restarted = client.post(f"/api/roles/{role['id']}/messages", json={"content": "$feed-manage 现在订阅了什么？查询后取消最后一个本机测试源。"})
            assert "FEED_HTTP_RESTART_VERIFIED" in restarted.text, restarted.text
            assert_feed_selected(sessions, role["id"])
            results = [item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]
            assert not any(item.isError for item in results), [(item.toolCallId, item.content) for item in results if item.isError]
            assert not any(item.toolName.endswith(("__poll_feeds", "__get_proactive_events", "__acknowledge_events")) for item in results)
            assert client.get("/api/mcp/servers").json()["servers"][0]["activeProcesses"] == 0
            evidence_dir = os.environ.get("MEIDO_FEED_EVIDENCE_DIR")
            if evidence_dir:
                target = Path(evidence_dir).resolve()
                target.mkdir(parents=True, exist_ok=True)
                (target / "http-native-evidence.json").write_text(json.dumps({"feedSHA": FEED_SHA,
                    "versions": versions, "serverId": server_id, "providerRequests": observations,
                    "httpFeedRequests": state["requests"], "firstResponse": response.text, "restartResponse": restarted.text,
                    "toolResults": [item.model_dump(mode="json") for item in results],
                    "serviceLogSHA256": hashlib.sha256((service / "feed_mcp.runtime.log").read_bytes()).hexdigest(),
                    "scope": "Scripted model boundary, actual main HTTP/SkillRegistry/SQLite/stdio/localhost RSS and Atom. No paid model or proactive calls."},
                    ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            client.close()


def test_real_feed_http_stale_cache_is_not_refresh_proof_and_revocation_denies_write(tmp_path, monkeypatch, installed_feed):
    executable, service, _ = installed_feed
    client, role, sessions, _ = setup_host(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    saved = client.post("/api/mcp/servers", json={"serverId": "actual_rss", "name": "本机失效来源",
        "command": [str(executable), str(service / "run_mcp.py")], "cwd": str(service),
        "roleIds": [role["id"]], "env": {"HOME": str(home), "USERPROFILE": str(home)}, "timeoutSeconds": 15})
    assert saved.status_code == 201, saved.text
    observations = []
    with local_feeds() as (base, state):
        class Adapter:
            turn = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                self.turn += 1
                observations.append({"turn": self.turn, "messages": messages})
                if self.turn == 1:
                    system = json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
                    assert "没有 stale 标记" in system and "不能据此声称已刷新" in system
                    yield ModelToolCall("find", "tool_search", {"query": "select:mcp_add"})
                elif self.turn == 2:
                    yield ModelToolCall("connect", "mcp_add", {"server_id": "actual_rss"})
                elif self.turn == 3:
                    yield ModelToolCall("discover", "tool_search", {"query": "select:mcp_actual_rss__feed_manage,mcp_actual_rss__feed_query"})
                elif self.turn == 4:
                    yield ModelToolCall("subscribe", "mcp_actual_rss__feed_manage", {"action": "subscribe", "name": "本机缓存源", "url": base+"/rss"})
                    yield ModelToolCall("populated", "mcp_actual_rss__feed_query", {"action": "latest"})
                elif self.turn == 5:
                    assert "RSS中文独有1" in provider_tool(messages, "populated")
                    state["fail"] = True
                    yield ModelToolCall("failed-refresh", "mcp_actual_rss__feed_query", {"action": "latest"})
                elif self.turn == 6:
                    assert "RSS中文独有1" in provider_tool(messages, "failed-refresh")
                    assert any(request["failed"] for request in state["requests"])
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido.test") as owner:
                        assert (await asyncio.wait_for(owner.delete("/api/mcp/servers/actual_rss"), 5)).status_code == 204
                    yield ModelToolCall("revoked-write", "mcp_actual_rss__feed_manage", {"action": "subscribe", "name": "不应新增", "url": base+"/atom"})
                else:
                    yield ModelTextDelta("FEED_FAILURE_VERIFIED 服务返回了缓存，无法确认刷新成功；授权已撤销，后续写入被拒绝。")

        monkeypatch.setattr(main, "model_adapter", Adapter())
        try:
            response = client.post(f"/api/roles/{role['id']}/messages", json={"content":
                f"$feed-manage 在本机失效来源订阅本机缓存源 {base}/rss，核对刷新失败与撤权；撤权后不得继续写入。"})
            assert "FEED_FAILURE_VERIFIED" in response.text, response.text
            assert_feed_selected(sessions, role["id"])
            results = [item for item in sessions.list_messages(f"role:{role['id']}") if item.messageType == "tool_result"]
            refreshed = next(item for item in results if item.toolCallId == "failed-refresh")
            revoked = next(item for item in results if item.toolCallId == "revoked-write")
            assert not refreshed.isError and "RSS中文独有1" in refreshed.content
            assert revoked.isError and revoked.toolResult["status"] == "denied"
            assert client.get("/api/mcp/servers").json()["servers"] == []
            assert not any(request["path"] == "/atom" for request in state["requests"])
            log = (service / "feed_mcp.runtime.log").read_text(encoding="utf-8")
            assert "503 Server Error" in log and "success=0 failed=1" in log
            evidence_dir = os.environ.get("MEIDO_FEED_EVIDENCE_DIR")
            if evidence_dir:
                target = Path(evidence_dir).resolve()
                target.mkdir(parents=True, exist_ok=True)
                (target / "http-failure-evidence.json").write_text(json.dumps({"feedSHA": FEED_SHA,
                    "providerRequests": observations, "httpFeedRequests": state["requests"], "response": response.text,
                    "toolResults": [item.model_dump(mode="json") for item in results]}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            client.close()
