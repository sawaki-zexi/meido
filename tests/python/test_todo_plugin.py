import asyncio
from datetime import datetime, timezone
import sqlite3

from backend.app.models import AgentRun
from backend.app.models import Message
from backend.app.memory_service import MemoryService
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.memory_store import MemoryStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, PluginRegistry, ToolContext
from backend.app.todo.agent import TodoReadPort, register_todo_agent_plugin
from backend.app.todo.plugin import TodoPlugin
from backend.app.todo.store import TodoStore
from fastapi.testclient import TestClient
from backend.app import main


def _run(run_id: str) -> AgentRun:
    return AgentRun(
        runId=run_id,
        roleId="role-1",
        sessionKey="role:role-1",
        status="created",
        startedAt=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )


def test_completed_run_atomically_creates_replayable_todo_outbox(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    sessions.open_role_session("role-1")

    _, failed_user, failed_assistant = sessions.create_run_with_messages(_run("failed"), "普通分享")
    sessions.finish_run(
        "failed", status="failed", assistant_message_id=failed_assistant.id,
        assistant_content="中断", turn_count=1,
    )
    assert sessions.list_todo_outbox() == []

    _, user, assistant = sessions.create_run_with_messages(_run("completed"), "提醒我周五交报告")
    sessions.finish_run(
        "completed", status="completed", assistant_message_id=assistant.id,
        assistant_content="好的", turn_count=1,
    )
    event, = sessions.list_todo_outbox()
    with sqlite3.connect(sessions.database_path) as connection:
        outbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(todo_turn_outbox)")}
    assert "user_content" not in outbox_columns
    assert event == {
        "eventId": "turn:completed",
        "runId": "completed",
        "roleId": "role-1",
        "sessionKey": "role:role-1",
        "userMessageId": user.id,
        "assistantMessageId": assistant.id,
        "userContent": "提醒我周五交报告",
        "userCreatedAt": user.createdAt.isoformat(),
        "attempts": 0,
        "lastError": None,
    }

    sessions.finish_run(
        "completed", status="completed", assistant_message_id=assistant.id,
        assistant_content="好的", turn_count=1,
    )
    assert len(sessions.list_todo_outbox()) == 1
    sessions.ack_todo_outbox("turn:completed")
    assert sessions.list_todo_outbox() == []


def test_legacy_outbox_migration_drops_copied_message_text(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE messages(message_id TEXT PRIMARY KEY,session_key TEXT,sequence INTEGER,role TEXT,content TEXT,status TEXT,created_at TEXT)")
        connection.execute("INSERT INTO messages VALUES('user-1','role:role-1',1,'user','提醒我交报告','completed','2026-10-08T00:00:00+00:00')")
        connection.execute("""
            CREATE TABLE todo_turn_outbox(
                event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE,role_id TEXT NOT NULL,session_key TEXT NOT NULL,
                user_message_id TEXT NOT NULL,assistant_message_id TEXT NOT NULL,user_content TEXT NOT NULL,
                user_created_at TEXT NOT NULL,created_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,next_attempt_at TEXT
            )
        """)
        connection.execute(
            "INSERT INTO todo_turn_outbox(event_id,run_id,role_id,session_key,user_message_id,assistant_message_id,user_content,user_created_at,created_at) "
            "VALUES('turn:legacy','legacy','role-1','role:role-1','user-1','assistant-1','提醒我交报告','2026-10-08T00:00:00+00:00','2026-10-08T00:00:00+00:00')"
        )

    initialize_databases(data)
    sessions = SessionStore(database)
    event, = sessions.list_todo_outbox()

    assert event["userContent"] == "提醒我交报告"
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(todo_turn_outbox)")}
    assert "user_content" not in columns


def test_todo_event_is_applied_once_and_date_reminder_uses_local_day(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    candidate = {
        "operation": "create",
        "title": "交实验报告",
        "dueDate": "2026-10-09",
        "reminderDate": "2026-10-09",
    }
    first = todos.apply_event(
        "turn:1", "role-1", "role:role-1", "user-1", [candidate], timezone_name="Asia/Shanghai",
    )
    again = todos.apply_event(
        "turn:1", "role-1", "role:role-1", "user-1", [candidate], timezone_name="Asia/Shanghai",
    )
    assert len(first) == 1
    assert again == first
    assert first[0]["status"] == "scheduled"
    assert first[0]["dueDate"] == "2026-10-09"
    assert first[0]["reminderAt"] is None

    before_local_day = datetime(2026, 10, 8, 15, 59, tzinfo=timezone.utc)
    at_local_day = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    assert todos.schedule_due_reminders(before_local_day) == []
    due, = todos.schedule_due_reminders(at_local_day)
    assert due["todoId"] == first[0]["id"]
    assert due["dueKey"].endswith(":date:2026-10-09")
    assert todos.schedule_due_reminders(at_local_day) == []


def test_similar_committed_create_intent_reuses_the_active_owner_todo(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    first, = todos.apply_event("turn:first", "role-1", "role:role-1", "message-1", [{"operation": "create", "title": "交实验报告"}])
    again, = todos.apply_event("turn:second", "role-2", "role:role-2", "message-2", [{
        "operation": "create", "title": "交实验报告", "dueDate": "2026-10-09",
    }])

    assert again["id"] == first["id"]
    assert again["status"] == "scheduled"
    assert again["dueDate"] == "2026-10-09"
    assert len(todos.list()) == 1


def test_exact_reminder_is_once_and_completed_todo_cannot_be_scheduled(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({
        "title": "提交申请",
        "dueDate": "2026-10-09",
        "reminderAt": "2026-10-08T17:00:00+08:00",
        "timezone": "Asia/Shanghai",
    }, source={"roleId": "role-1", "sessionKey": "role:role-1", "messageId": "user-1"})

    assert todos.schedule_due_reminders(datetime(2026, 10, 8, 8, 59, tzinfo=timezone.utc)) == []
    due, = todos.schedule_due_reminders(datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc))
    assert due["todoId"] == item["id"]
    assert due["status"] == "pending"
    assert due["dueKey"].endswith(":at:2026-10-08T09:00:00+00:00")
    assert todos.schedule_due_reminders(datetime(2026, 10, 8, 9, 1, tzinfo=timezone.utc)) == []
    delivered, = todos.deliver_pending_reminders()
    assert delivered["status"] == "delivered"
    assert todos.deliver_pending_reminders() == []

    todos.set_status(item["id"], "completed")
    assert todos.list_reminders()[0]["status"] == "delivered"


def test_owner_todos_survive_role_source_deletion_and_can_be_restored(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create(
        {"title": "买牛奶", "timezone": "Asia/Shanghai"},
        source={"roleId": "deleted-role", "sessionKey": "role:deleted-role", "messageId": "message-1"},
    )
    todos.set_status(item["id"], "cancelled")
    restored = todos.set_status(item["id"], "inbox")
    todos.mark_source_unavailable("deleted-role")

    assert restored["status"] == "inbox"
    assert restored["sourceRoleId"] == "deleted-role"
    assert todos.get(item["id"])["sourceAvailable"] is False


def test_extractor_accepts_explicit_reminder_but_rejects_ordinary_sharing():
    event = {
        "userContent": "提醒我周五交实验报告",
        "userCreatedAt": "2026-10-08T00:00:00+00:00",
        "timezone": "Asia/Shanghai",
    }
    candidate, = TodoPlugin.extract_explicit_intent(event, [])
    assert candidate["title"] == "交实验报告"
    assert candidate["dueDate"] == "2026-10-09"
    assert candidate["reminderDate"] == "2026-10-09"
    assert "reminderAt" not in candidate
    assert TodoPlugin.extract_explicit_intent(
        {**event, "userContent": "我下周可能要搬家"}, [],
    ) == []
    assert TodoPlugin.extract_explicit_intent(
        {**event, "userContent": "我可能要搬家"}, [],
    ) == []
    assert TodoPlugin.extract_explicit_intent(
        {**event, "userContent": "不用提醒我交报告"}, [],
    ) == []


def test_candidate_action_and_dates_must_match_user_message(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({"title": "交实验报告"})
    active = todos.list()
    event = {
        "userContent": "提醒我明天交实验报告",
        "userCreatedAt": "2026-10-08T00:00:00+00:00",
        "timezone": "Asia/Shanghai",
    }

    candidates = TodoPlugin._validate_candidates(event, active, [
        {"operation": "complete", "todoId": item["id"]},
        {"operation": "create", "title": "交实验报告", "dueDate": "2030-01-01", "reminderAt": "2030-01-01T12:00:00+08:00"},
    ])

    assert len(candidates) == 1
    assert candidates[0]["operation"] == "create"
    assert candidates[0]["dueDate"] is None
    assert candidates[0]["reminderAt"] is None


def test_default_extractor_completes_only_an_unambiguous_owner_todo(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({"title": "交实验报告"})
    event = {"userContent": "帮我完成交实验报告", "timezone": "Asia/Shanghai"}

    [candidate] = TodoPlugin.extract_explicit_intent(event, todos.list())
    applied = todos.apply_event("complete-one", "role-1", "role:role-1", "message-1", [candidate])

    assert applied[0]["id"] == item["id"]
    assert applied[0]["status"] == "completed"


def test_default_extractor_uses_replacement_date_when_rescheduling(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({"title": "交实验报告", "dueDate": "2026-10-09"})
    event = {
        "userContent": "请把交实验报告改到周六",
        "userCreatedAt": "2026-10-08T00:00:00+00:00",
        "timezone": "Asia/Shanghai",
    }

    [candidate] = TodoPlugin.extract_explicit_intent(event, todos.list())
    [updated] = todos.apply_event("reschedule", "role-1", "role:role-1", "message-1", [candidate])

    assert updated["id"] == item["id"]
    assert updated["dueDate"] == "2026-10-10"


def test_worker_requires_explicit_cue_and_rejects_unrelated_model_candidate(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    sessions.open_role_session("role-1")
    _, _, assistant = sessions.create_run_with_messages(_run("ordinary"), "我下周可能要搬家")
    sessions.finish_run("ordinary", status="completed", assistant_message_id=assistant.id, assistant_content="嗯", turn_count=1)
    todos = TodoStore(tmp_path / "todos.db")
    plugin = TodoPlugin(todos, sessions, extractor=lambda event, _: [{
        "operation": "create",
        "title": "预约体检" if "搬家" in str(event.get("userContent")) else "交报告",
    }])

    assert plugin.process_pending_once() == 1
    assert todos.list() == []

    _, _, assistant = sessions.create_run_with_messages(_run("explicit"), "提醒我周五交报告")
    sessions.finish_run("explicit", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    assert plugin.process_pending_once() == 1
    assert [item["title"] for item in todos.list()] == ["交报告"]


def test_ambiguous_todo_target_is_left_for_review(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交实验报告"})
    todos.create({"title": "交课程报告"})
    event = {"userContent": "提醒我完成报告", "timezone": "Asia/Shanghai"}
    candidate = {"operation": "complete", "todoId": todos.list()[0]["id"]}

    [normalized] = TodoPlugin._validate_candidates(event, todos.list(), [candidate])
    assert normalized["todoId"] is None
    todos.apply_event("ambiguous", "role-1", "role:role-1", "message-1", [normalized])
    diagnostic, = todos.operation_diagnostics()
    assert diagnostic["result"] == "needs_review"


def test_reminder_delivery_failure_stays_failed_until_explicit_retry(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交报告", "reminderDate": "2000-01-01", "timezone": "Asia/Shanghai"})
    todos.schedule_due_reminders(datetime(2026, 10, 9, tzinfo=timezone.utc))
    with sqlite3.connect(todos.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_todo_inbox BEFORE INSERT ON todo_reminder_inbox BEGIN SELECT RAISE(ABORT, 'inbox unavailable'); END")

    assert todos.deliver_pending_reminders() == []
    reminder, = todos.list_reminders()
    assert reminder["status"] == "failed"
    assert reminder["deliveredAt"] is None
    with sqlite3.connect(todos.database_path) as connection:
        connection.execute("DROP TRIGGER fail_todo_inbox")
    assert todos.retry_reminder(reminder["id"]) is True
    delivered, = todos.deliver_pending_reminders()
    assert delivered["id"] == reminder["id"]
    assert todos.list_reminders()[0]["status"] == "delivered"


def test_worker_replays_outbox_through_event_idempotency(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    sessions.open_role_session("role-1")
    _, user, assistant = sessions.create_run_with_messages(_run("worker"), "提醒我周五交报告")
    sessions.finish_run("worker", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    todos = TodoStore(tmp_path / "todos.db")
    plugin = TodoPlugin(todos, sessions)

    assert plugin.process_pending_once() == 1
    assert plugin.process_pending_once() == 0
    [item] = todos.list()
    assert item["sourceMessageId"] == user.id
    assert item["title"] == "交报告"
    assert sessions.list_todo_outbox() == []


def test_completed_todo_does_not_receive_a_future_reminder(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({
        "title": "交文件", "reminderDate": "2026-10-09", "timezone": "Asia/Shanghai",
    })
    todos.set_status(item["id"], "completed")

    assert todos.schedule_due_reminders(datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)) == []
    assert todos.list_reminders() == []


def test_related_context_only_contains_matching_active_todos(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交实验报告"})
    todos.create({"title": "买牛奶"})
    plugin = TodoPlugin(todos, SessionStore(tmp_path / "sessions.db"))

    assert "交实验报告" in plugin.context_block("今晚需要交实验报告")
    assert "交实验报告" in plugin.context_block("报告什么时候交")
    assert plugin.context_block("今天心情不错") == ""


def test_context_includes_overdue_items_for_overdue_queries(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    overdue = todos.create({"title": "提交旧报告", "dueDate": "2000-01-01", "timezone": "Asia/Shanghai"})
    todos.create({"title": "提交新报告", "dueDate": "2999-01-01", "timezone": "Asia/Shanghai"})

    results = todos.read_port("有哪些逾期待办", limit=5)

    assert results[0]["id"] == overdue["id"]
    assert len(results) == 1


def test_todo_api_crud_filters_and_persistent_reminder_inbox(tmp_path, monkeypatch):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    plugin = TodoPlugin(todos, sessions)
    monkeypatch.setattr(main, "todo_store", todos)
    monkeypatch.setattr(main, "todo_plugin", plugin)
    client = TestClient(main.app)

    settings = client.get("/api/todos/settings")
    assert settings.status_code == 200
    assert settings.json()["remindersEnabled"] is True
    paused = client.put("/api/todos/settings", json={"remindersEnabled": False})
    assert paused.status_code == 200
    assert paused.json() == {"enabled": True, "remindersEnabled": False, "timezone": "Asia/Shanghai", "timezoneConfigured": False}
    resumed = client.put("/api/todos/settings", json={"remindersEnabled": True})
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True

    created = client.post("/api/todos", json={
        "title": "交申请", "dueDate": "2020-01-01", "reminderDate": "2020-01-01", "timezone": "Asia/Shanghai",
    })
    assert created.status_code == 201
    todo = created.json()
    assert todo["status"] == "scheduled"
    assert client.get("/api/todos?status=scheduled").json()["todos"] == [todo]
    reminders = client.get("/api/todos/reminders").json()
    assert reminders["unreadCount"] == 1
    assert reminders["reminders"][0]["todoId"] == todo["id"]
    assert client.post(f"/api/todos/reminders/{reminders['reminders'][0]['id']}/read").status_code == 204
    assert client.get("/api/todos/reminders").json()["unreadCount"] == 0

    assert client.put(f"/api/todos/{todo['id']}/status", json={"status": "cancelled"}).json()["status"] == "cancelled"
    assert client.put(f"/api/todos/{todo['id']}", json={"title": "更新申请"}).json()["title"] == "更新申请"
    assert client.put(f"/api/todos/{todo['id']}/status", json={"status": "inbox"}).json()["status"] == "inbox"
    assert client.get("/api/todos?status=invalid").status_code == 422
    assert client.post("/api/todos", json={
        "title": "无效提醒", "reminderDate": "2026-10-09", "reminderAt": "2026-10-09T10:00:00+08:00",
    }).status_code == 422
    inbox = client.post("/api/todos", json={"title": "临时事项"}).json()
    scheduled = client.put(f"/api/todos/{inbox['id']}", json={"dueDate": "2026-10-10"}).json()
    assert scheduled["status"] == "scheduled"
    returned_to_inbox = client.put(f"/api/todos/{inbox['id']}", json={
        "dueDate": None, "dueAt": None, "reminderAt": None, "reminderDate": None,
    }).json()
    assert returned_to_inbox["status"] == "inbox"


def test_todo_search_requires_plugin_and_read_capability_and_is_read_only(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交实验报告"})
    registry = PluginRegistry()
    register_todo_agent_plugin(registry)
    capabilities = asyncio.run(CapabilityRegistry(plugins=registry).resolve_async(
        role_id="role-1", session_key="role:role-1", run_id="run-1",
        enabled_tools={"todo.search"}, enabled_plugin_ids={"todo"},
        granted_capabilities={"todo.read"}, host_services={"todo.read": TodoReadPort(todos)},
    ))
    tool = capabilities.get("todo.search")
    assert tool is not None
    assert capabilities.definition("todo.search").risk == "read_only"
    assert "ownerId" not in capabilities.definition("todo.search").input_schema["properties"]
    result = asyncio.run(tool.execute(
        {"query": "实验报告"},
        ToolContext("role-1", "role:role-1", "run-1", CancellationToken()),
    ))
    assert "交实验报告" in result.content
    assert capabilities.get("todo.create") is None
    asyncio.run(capabilities.close())


def test_disabled_plugin_keeps_unprocessed_outbox_and_existing_todos(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    sessions.open_role_session("role-1")
    _, _, assistant = sessions.create_run_with_messages(_run("disabled"), "提醒我周五交报告")
    sessions.finish_run("disabled", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    todos = TodoStore(tmp_path / "todos.db")
    saved = todos.create({"title": "已经存在"})
    todos.update_settings(enabled=False)
    plugin = TodoPlugin(todos, sessions)

    assert plugin.process_pending_once() == 0
    assert len(sessions.list_todo_outbox()) == 1
    assert todos.get(saved["id"]) is not None


def test_reminders_can_be_paused_without_disabling_todos(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    plugin = TodoPlugin(todos, sessions)
    todo = todos.create({"title": "交报告", "reminderDate": "2020-01-01", "timezone": "Asia/Shanghai"})

    assert todos.settings()["remindersEnabled"] is True
    todos.update_settings(reminders_enabled=False)
    assert todos.settings()["enabled"] is True
    assert plugin.schedule_reminders_once() == []
    assert todos.list_reminders() == []
    assert todos.get(todo["id"])["title"] == "交报告"

    todos.update_settings(reminders_enabled=True)
    delivered = plugin.schedule_reminders_once()
    assert len(delivered) == 1
    assert delivered[0]["todoId"] == todo["id"]
    assert todos.list_reminders()[0]["status"] == "delivered"


def test_pausing_reminders_preserves_existing_inbox_events(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    plugin = TodoPlugin(todos, sessions)
    todo = todos.create({"title": "保留提醒", "reminderDate": "2020-01-01", "timezone": "Asia/Shanghai"})
    todos.schedule_due_reminders(datetime(2020, 1, 1, tzinfo=timezone.utc))
    todos.update_settings(reminders_enabled=False)

    assert plugin.schedule_reminders_once() == []
    reminder, = todos.list_reminders()
    assert reminder["todoId"] == todo["id"]
    assert reminder["status"] == "pending"


def test_date_reminder_delivered_after_due_day_is_marked_delayed(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交报告", "reminderDate": "2026-10-09", "timezone": "Asia/Shanghai"})

    due_moment = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    todos.schedule_due_reminders(due_moment)
    todos.deliver_pending_reminders(now=due_moment)

    reminder, = todos.list_reminders()
    assert reminder["delayed"] is True
    assert reminder["status"] == "delivered"


def test_todo_request_is_not_saved_as_role_memory(tmp_path):
    memories = MemoryStore(tmp_path / "memory.db")
    service = MemoryService(
        memories,
        post_response_provider=lambda *args: [{"memoryType": "fact", "summary": "周五要交实验报告"}],
    )
    user = Message(
        id="message-user", sessionKey="role:role-1", sequence=1, role="user",
        content="提醒我周五交实验报告", status="completed", createdAt=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )
    assistant = Message(
        id="message-assistant", sessionKey="role:role-1", sequence=2, role="assistant",
        content="好，我记下了。", status="completed", createdAt=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )

    assert service.process_turn("role-1", "role:role-1", user, assistant) == []
    assert memories.list_all("role-1") == []


def test_todo_turn_is_excluded_from_recent_memory_document(tmp_path):
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    sessions.open_role_session("role-1")
    _, _, assistant = sessions.create_run_with_messages(_run("maintenance"), "提醒我周五交实验报告")
    sessions.finish_run("maintenance", status="completed", assistant_message_id=assistant.id, assistant_content="好，我会提醒你。", turn_count=1)
    maintenance = MemoryMaintenance(tmp_path / "roles", sessions)

    maintenance.maintain("role-1", "role:role-1")

    recent = (tmp_path / "roles" / "role-1" / "memory" / "RECENT_CONTEXT.md").read_text(encoding="utf-8")
    assert "提醒我周五交实验报告" not in recent
    assert "好，我会提醒你" not in recent
