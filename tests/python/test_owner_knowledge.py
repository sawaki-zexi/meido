import asyncio
import io
import json
import logging
import sqlite3
import zipfile
from types import SimpleNamespace
import pytest
import httpx

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.owner_knowledge import (
    DocumentScan,
    FeishuApiConnector,
    OAuthCallbackAccessLogFilter,
    OwnerKnowledgePlugin,
    OwnerKnowledgeStore,
    OwnerKnowledgeTool,
    RoleBoundOwnerKnowledgeReader,
    RemoteDocument,
    _docx_text,
)
from backend.app.storage import resolve_owner_knowledge_database


def test_owner_knowledge_database_uses_dedicated_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", raising=False)
    project_root = tmp_path / "meido"
    data_root = project_root / ".data"

    database_path = resolve_owner_knowledge_database(project_root, data_root)

    assert database_path == project_root / "owner-knowledge-data" / "owner-knowledge.db"
    assert database_path.parent.name == "owner-knowledge-data"


def test_owner_knowledge_database_migrates_legacy_database_once(tmp_path, monkeypatch):
    monkeypatch.delenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", raising=False)
    project_root = tmp_path / "meido"
    data_root = project_root / ".data"
    data_root.mkdir(parents=True)
    legacy = data_root / "owner-knowledge.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('legacy')")

    migrated = resolve_owner_knowledge_database(project_root, data_root)
    overridden = resolve_owner_knowledge_database(project_root, data_root)

    assert migrated.exists()
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("legacy",)
    with sqlite3.connect(migrated) as connection:
        connection.execute("INSERT INTO marker VALUES ('after-migration')")
    assert overridden == migrated
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT COUNT(*) FROM marker").fetchone() == (2,)


def test_owner_knowledge_database_honors_explicit_directory(tmp_path, monkeypatch):
    explicit_root = tmp_path / "custom-owner-data"
    monkeypatch.setenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", str(explicit_root))

    database_path = resolve_owner_knowledge_database(tmp_path / "meido", tmp_path / ".data")

    assert database_path == explicit_root / "owner-knowledge.db"


class FakeFeishu:
    def __init__(self):
        self.documents = []
        self.contents = {}
        self.read_ids = []
        self.complete = True

    async def scan(self):
        return DocumentScan(documents=list(self.documents), complete=self.complete)

    async def read_document(self, document):
        self.read_ids.append(document.document_id)
        return self.contents[document.document_id]


class FakeEmbedder:
    model_id = "test-embedder-v1"

    def __init__(self):
        self.document_calls = []
        self.query_calls = []

    def embed_documents(self, texts):
        self.document_calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        self.query_calls.append(text)
        return [1.0, 0.0]


class FakeCredentialStore:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


def make_document(document_id, *, title=None, updated_at="1", obj_type="docx"):
    return RemoteDocument(
        document_id=document_id,
        node_id=f"node-{document_id}",
        object_token=f"obj-{document_id}",
        object_type=obj_type,
        title=title or document_id,
        url=f"https://example.test/wiki/{document_id}",
        updated_at=updated_at,
    )


def make_plugin(tmp_path):
    store = OwnerKnowledgeStore(tmp_path / "owner-knowledge.db", credential_store=FakeCredentialStore())
    store.set_state("feishu_access_token", "test-token")
    feishu = FakeFeishu()
    embedder = FakeEmbedder()
    plugin = OwnerKnowledgePlugin(store, feishu, embedder)
    return plugin, store, feishu, embedder


def test_pause_preserves_the_local_index_and_blocks_retrieval(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1", title="学习日记")]
        feishu.contents["diary-1"] = "今天我开始学习 Python 编程。"

        await plugin.enable()
        before = await plugin.search("编程", role_id="role-a")
        assert before

        await plugin.pause()
        after = await plugin.search("编程", role_id="role-a")

        assert after == []
        assert plugin.status().enabled is False
        assert [item.document_id for item in store.list_documents()] == ["diary-1"]

    asyncio.run(scenario())


def test_failed_resume_keeps_plugin_paused_and_does_not_expose_stale_data(tmp_path):
    async def scenario():
        plugin, _, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "已经同步的内容"
        await plugin.enable()
        await plugin.pause()

        feishu.complete = False
        try:
            await plugin.enable()
        except RuntimeError:
            pass
        else:
            raise AssertionError("an incomplete catch-up scan must fail to resume")

        assert plugin.status().enabled is False
        assert await plugin.search("内容", role_id="role-a") == []

    asyncio.run(scenario())


def test_incomplete_scan_never_confirms_remote_deletions(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("old"), make_document("current")]
        feishu.contents.update({"old": "旧日记", "current": "当前日记"})
        await plugin.enable()

        feishu.documents = [make_document("current")]
        feishu.complete = False
        report = await plugin.sync()

        assert report.deleted == 0
        assert {item.document_id for item in store.list_documents()} == {"old", "current"}

    asyncio.run(scenario())


def test_pause_during_sync_finishes_current_document_and_leaves_remaining_work(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        first = make_document("first", updated_at="1")
        second = make_document("second", updated_at="1")
        feishu.documents = [first, second]
        feishu.contents = {"first": "旧第一篇", "second": "旧第二篇"}
        await plugin.enable()
        feishu.documents = [
            make_document("first", updated_at="2"),
            make_document("second", updated_at="2"),
        ]
        feishu.contents = {"first": "新第一篇", "second": "新第二篇"}
        started = asyncio.Event()
        release = asyncio.Event()
        original_read = feishu.read_document
        reads = []

        async def blocking_read(document):
            reads.append(document.document_id)
            if document.document_id == "first":
                started.set()
                await release.wait()
            return await original_read(document)

        feishu.read_document = blocking_read
        sync_task = asyncio.create_task(plugin.sync())
        await started.wait()
        await plugin.pause()
        release.set()
        report = await sync_task

        assert report.complete is False
        assert report.deleted == 0
        assert reads == ["first"]
        assert {item.document_id for item in store.list_documents()} == {"first", "second"}
        assert store.document_content_hash("first")[0] != store.document_content_hash("second")[0]

    asyncio.run(scenario())


def test_unchanged_documents_are_not_read_or_embedded_again(tmp_path):
    async def scenario():
        plugin, _, feishu, embedder = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1", updated_at="1700000000")]
        feishu.contents["diary-1"] = "今天学习了新的概念。"
        await plugin.enable()
        original_reads = list(feishu.read_ids)
        original_embeddings = list(embedder.document_calls)

        report = await plugin.sync()

        assert report.unchanged == 1
        assert feishu.read_ids == original_reads
        assert embedder.document_calls == original_embeddings

    asyncio.run(scenario())


def test_embedding_model_change_rebuilds_existing_vectors_even_when_content_is_unchanged(tmp_path):
    async def scenario():
        plugin, _, feishu, embedder = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "同一篇日记内容"
        await plugin.enable()
        embedder.model_id = "test-embedder-v2"
        report = await plugin.sync()

        assert report.updated == 1
        assert len(embedder.document_calls) == 2

    asyncio.run(scenario())


def test_runtime_tool_is_read_only_and_uses_the_bound_role():
    class Reader:
        def __init__(self):
            self.role_ids = []

        async def search(self, query, *, role_id, limit):
            self.role_ids.append(role_id)
            return [{"kind": "owner_knowledge", "text": query}]

    async def scenario():
        reader = Reader()
        tool = OwnerKnowledgeTool(reader)
        context = ToolContext(
            role_id="role-b",
            session_key="role:role-b",
            run_id="run-1",
            signal=CancellationToken(),
        )

        result = await tool.execute({"query": "主人怎么开始学编程的"}, context)

        assert tool.definition.risk == "read_only"
        assert "roleId" not in tool.definition.input_schema["properties"]
        assert reader.role_ids == ["role-b"]
        assert json.loads(result.content)["results"][0]["kind"] == "owner_knowledge"

    asyncio.run(scenario())


def test_role_bound_reader_rejects_a_different_runtime_role():
    reader = RoleBoundOwnerKnowledgeReader("role-a", lambda *args, **kwargs: [])

    with pytest.raises(ValueError, match="bound to"):
        asyncio.run(reader.search("query", role_id="role-b", limit=2))


def test_understanding_is_role_scoped_and_failed_regeneration_preserves_previous(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "主人持续学习并喜欢先弄清原理。"
        await plugin.enable()
        role_a = SimpleNamespace(
            id="role-a",
            name="沉静的角色",
            profile=SimpleNamespace(profile="重视证据", personality="克制", behaviorRules="先观察"),
        )

        class WorkingModel:
            async def complete_messages(self, messages, configuration, *, max_tokens=None):
                del messages, configuration, max_tokens
                return "我觉得主人会耐心追问概念之间的联系。"

        first = await plugin.generate_understanding(role_a, WorkingModel(), None, "记忆只读上下文")
        assert first["version"] == 1
        role_a_hits = await plugin.search("主人学习习惯", role_id="role-a")
        role_b_hits = await plugin.search("主人学习习惯", role_id="role-b")
        assert any(item.kind == "role_understanding" for item in role_a_hits)
        assert not any(item.kind == "role_understanding" for item in role_b_hits)

        class FailingModel:
            async def complete_messages(self, messages, configuration, *, max_tokens=None):
                raise RuntimeError("offline")

        try:
            await plugin.generate_understanding(role_a, FailingModel(), None, "")
        except RuntimeError:
            pass
        assert store.get_understanding("role-a")["body"] == first["body"]
        assert store.pending_understandings() == []
        store.resume_failed_understandings()
        assert store.pending_understandings() == ["role-a"]

    asyncio.run(scenario())


def test_disconnect_clears_owner_data_and_vectors_but_keeps_feishu_app_configuration(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        store.set_state("feishu_app_id", "app-id")
        store.set_state("feishu_app_secret", "app-secret")
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "本地资料内容"
        await plugin.enable()

        store.clear_source()

        assert store.list_documents() == []
        assert store.get_state("feishu_access_token") == ""
        assert store.get_state("feishu_app_id") == "app-id"
        assert store.get_state("feishu_app_secret") == "app-secret"

    asyncio.run(scenario())


def test_exported_docx_content_keeps_paragraph_boundaries():
    buffer = io.BytesIO()
    document_xml = (
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        "<w:body><w:p><w:r><w:t>第一段内容</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>第二段内容</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    assert _docx_text(buffer.getvalue()) == "第一段内容\n\n第二段内容"


def test_feishu_connector_exchanges_authorization_code_for_user_token(monkeypatch):
    calls = []

    def handler(request):
        calls.append((str(request.url), json.loads(request.content.decode("utf-8"))))
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app-token"})
        return httpx.Response(200, json={"code": 0, "data": {
            "access_token": "user-token",
            "refresh_token": "user-refresh-token",
            "expires_in": 7200,
        }})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.owner_knowledge.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    connector = FeishuApiConnector(lambda: "")

    payload = asyncio.run(connector.exchange_code(
        "auth-code",
        "app-id",
        "app-secret",
        "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
    ))

    assert payload["access_token"] == "user-token"
    assert payload["refresh_token"] == "user-refresh-token"
    token_request = next(body for url, body in calls if url.endswith("/authen/v2/oauth/token"))
    assert token_request["grant_type"] == "authorization_code"
    assert token_request["code"] == "auth-code"
    assert token_request["redirect_uri"] == "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback"


def test_feishu_connector_refreshes_user_token(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app-token"})
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "fresh-token"}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.owner_knowledge.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    connector = FeishuApiConnector(lambda: "")

    payload = asyncio.run(connector.refresh_token("old-refresh-token", "app-id", "app-secret"))

    assert payload["access_token"] == "fresh-token"


def test_feishu_oauth_rejection_reports_safe_response_code(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "app-token"})
        return httpx.Response(400, json={"code": 20015, "msg": "invalid grant"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.owner_knowledge.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    connector = FeishuApiConnector(lambda: "")

    with pytest.raises(RuntimeError, match=r"HTTP 400.*20015") as error:
        asyncio.run(connector.exchange_code(
            "one-time-auth-code",
            "app-id",
            "app-secret",
            "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
        ))

    assert "one-time-auth-code" not in str(error.value)
    assert "app-secret" not in str(error.value)


def test_oauth_callback_access_log_redacts_code_and_state():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "server.py",
        1,
        '%s - "%s %s HTTP/%s" %s',
        (
            "127.0.0.1:12345",
            "GET",
            "/api/owner-knowledge/oauth/callback?code=secret-code&state=secret-state",
            "1.1",
            502,
        ),
        None,
    )

    assert OAuthCallbackAccessLogFilter().filter(record)
    assert "secret-code" not in record.getMessage()
    assert "secret-state" not in record.getMessage()
    assert "?<redacted>" in record.getMessage()


def test_chinese_lexical_search_matches_partial_phrases(tmp_path):
    async def scenario():
        plugin, store, feishu, embedder = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "主人通过持续练习，逐渐掌握 Python 编程。"
        await plugin.enable()

        hits = store.search("持续掌握", [1.0, 0.0], embedder.model_id, 5)

        assert hits
        assert hits[0].document_id == "diary-1"

    asyncio.run(scenario())


def test_sqlite_vec_index_round_trips_vectors_when_extension_is_available(tmp_path):
    store = OwnerKnowledgeStore(tmp_path / "vector-index.db")
    if store.vector_index is None:
        pytest.skip("sqlite-vec extension is unavailable")

    store.vector_index.upsert("chunk-1", "model-v1", [1.0, 0.0])

    assert store.vector_index.search("model-v1", [1.0, 0.0], 1) == [("chunk-1", 1.0)]


def test_sensitive_feishu_credentials_are_stored_outside_owner_database(tmp_path):
    credential_store = FakeCredentialStore()
    store = OwnerKnowledgeStore(tmp_path / "owner-knowledge.db", credential_store=credential_store)
    store.set_state("feishu_app_secret", "app-secret-value")
    store.set_state("feishu_refresh_token", "refresh-token-value")

    with store._connect() as connection:
        stored_state = connection.execute("SELECT key, value FROM owner_plugin_state").fetchall()

    assert stored_state == []
    assert credential_store.get("feishu_app_secret") == "app-secret-value"
    assert credential_store.get("feishu_refresh_token") == "refresh-token-value"


def test_feishu_connector_paginates_and_recursively_enumerates_document_nodes(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.url.path, request.url.params.get("page_token"), request.url.params.get("parent_node_token")))
        path = request.url.path
        if path.endswith("/wiki/v2/spaces"):
            if request.url.params.get("page_token") == "spaces-page-2":
                data = {"items": [{"space_id": "space-2"}], "has_more": False}
            else:
                data = {"items": [{"space_id": "space-1"}], "has_more": True, "page_token": "spaces-page-2"}
        elif path.endswith("/wiki/v2/spaces/space-1/nodes"):
            parent = request.url.params.get("parent_node_token")
            if parent == "folder-node":
                data = {"items": [{
                    "node_token": "doc-node",
                    "obj_token": "doc-token",
                    "obj_type": "docx",
                    "title": "学习记录",
                    "obj_edit_time": "1700000000",
                    "has_child": False,
                }], "has_more": False}
            else:
                data = {"items": [{"node_token": "folder-node", "obj_token": "folder-node", "obj_type": "folder", "title": "资料夹", "has_child": True}], "has_more": False}
        elif path.endswith("/wiki/v2/spaces/space-2/nodes"):
            data = {"items": [{
                "node_token": "sheet-node",
                "obj_token": "sheet-token",
                "obj_type": "sheet",
                "title": "學習表格",
                "has_child": False,
            }], "has_more": False}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 0, "data": data})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.app.owner_knowledge.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    connector = FeishuApiConnector(lambda: "user-token")

    result = asyncio.run(connector.scan())

    assert result.complete is True
    assert [(item.document_id, item.object_type) for item in result.documents] == [
        ("doc-token", "docx"),
        ("sheet-token", "sheet"),
    ]
    assert any(token == "spaces-page-2" for _, token, _ in calls)
    assert any(parent == "folder-node" for _, _, parent in calls)


def test_truncated_feishu_pagination_does_not_confirm_missing_documents(tmp_path, monkeypatch):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("existing")]
        feishu.contents["existing"] = "之前同步的内容"
        await plugin.enable()

        def handler(request):
            if request.url.path.endswith("/wiki/v2/spaces"):
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {"items": [{"space_id": "space-1"}], "has_more": True},
                })
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

        original_client = httpx.AsyncClient
        monkeypatch.setattr(
            "backend.app.owner_knowledge.httpx.AsyncClient",
            lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
        )
        plugin.connector = FeishuApiConnector(lambda: "user-token")

        report = await plugin.sync()

        assert report.complete is False
        assert report.deleted == 0
        assert [item.document_id for item in store.list_documents()] == ["existing"]

    asyncio.run(scenario())


def test_chunk_listing_can_page_past_ten_thousand_chunks(tmp_path):
    store = OwnerKnowledgeStore(tmp_path / "large-owner-knowledge.db", credential_store=FakeCredentialStore())
    with store._connect() as connection:
        connection.execute(
            """INSERT INTO owner_documents(
                document_id, node_id, object_token, object_type, title, url, updated_at,
                content, content_hash, embedding_model, status, synced_at
            ) VALUES ('large', 'node', 'object', 'docx', 'large', 'https://example.test', '', '', '', 'test', 'synced', ?)""",
            ("2026-10-08T00:00:00+00:00",),
        )
        connection.executemany(
            """INSERT INTO owner_chunks(
                chunk_id, document_id, ordinal, body, location, embedding_json, embedding_model
            ) VALUES (?, 'large', ?, ?, '', '[1.0, 0.0]', 'test')""",
            ((f"chunk-{index}", index, f"body-{index}") for index in range(10005)),
        )

    assert store.count_chunks() == 10005
    final_page = store.list_chunks(limit=12, offset=10000)

    assert len(final_page) == 5
    assert final_page[-1].text == "body-10004"


def test_deleting_role_cancels_inflight_understanding_and_prevents_resurrection(tmp_path):
    async def scenario():
        plugin, store, feishu, _ = make_plugin(tmp_path)
        feishu.documents = [make_document("diary-1")]
        feishu.contents["diary-1"] = "主人持续学习。"
        await plugin.enable()
        started = asyncio.Event()
        role = SimpleNamespace(id="role-a", name="角色", profile=SimpleNamespace())

        class BlockingModel:
            async def complete_messages(self, messages, configuration, *, max_tokens=None):
                started.set()
                await asyncio.Event().wait()

        plugin.queue_understanding_refresh("role-a", role, BlockingModel(), None, "")
        await started.wait()
        await plugin.begin_role_deletion("role-a")
        store.delete_role("role-a")
        await plugin.end_role_deletion("role-a")
        await asyncio.sleep(0)

        assert store.get_understanding("role-a") is None
        assert store.understanding_job("role-a") is None
        assert all(task.get_name() != "owner-understanding:role-a" for task in plugin._understanding_workers)

    asyncio.run(scenario())
