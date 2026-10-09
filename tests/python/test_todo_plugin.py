import asyncio
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime import CancellationToken, CapabilityRegistry, PluginRegistry, ToolContext
from backend.app.models import AgentRun
from backend.app.storage import initialize_databases
from backend.app.todo.agent import TodoReminderControlTool, TodoWriteTool, register_todo_agent_plugin
from backend.app.todo.application import TodoApplicationService
from backend.app.todo.plugin import TodoPlugin
from backend.app.todo.store import TodoStore


def _run(run_id: str) -> AgentRun:
    return AgentRun(
        runId=run_id,
        roleId="role-1",
        sessionKey="role:role-1",
        status="created",
        startedAt=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )


def _sessions(tmp_path):
    initialize_databases(tmp_path / "data")
    from backend.app.session_store import SessionStore

    return SessionStore(tmp_path / "data" / "sessions.db")


def _plugin(tmp_path, *, roles=("role-1",)):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    sessions.open_role_session("role-1")
    plugin = TodoPlugin(
        todos,
        sessions,
        reminder_sender=lambda role_id, _event_id, kind, payload: f"{role_id}:{kind}:" + "、".join(str(item["title"]) for item in payload),
        available_roles=lambda: list(roles),
    )
    return todos, sessions, plugin


def test_role_tool_and_web_share_the_same_owner_todo_rules(tmp_path, monkeypatch):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    sessions.open_role_session("role-1")
    _, user_message, _ = sessions.create_run_with_messages(_run("todo-write"), "提醒我周五提交实验报告")
    application = TodoApplicationService(todos, sessions)
    tool = TodoWriteTool(application)
    context = ToolContext("role-1", "role:role-1", "todo-write", CancellationToken(), "call-create")

    created = tool.execute({"operation": "create", "title": "提交实验报告", "dueDate": "2026-10-09"}, context)

    assert created.is_error is False
    item = todos.list()[0]
    assert item["title"] == "提交实验报告"
    assert item["sourceRoleId"] == "role-1"
    assert item["sourceMessageId"] == user_message.id
    assert tool.execute({"operation": "create", "title": "提交实验报告", "dueDate": "2026-10-09"}, context).is_error is False
    assert len(todos.list()) == 1

    updated = tool.execute(
        {"operation": "update", "targetTitle": "提交实验报告", "title": "提交报告（角色更新）"},
        ToolContext("role-1", "role:role-1", "todo-write", CancellationToken(), "call-update"),
    )
    assert updated.is_error is False
    assert todos.get(item["id"])["title"] == "提交报告（角色更新）"
    completed = tool.execute(
        {"operation": "complete", "targetTitle": "提交报告（角色更新）"},
        ToolContext("role-1", "role:role-1", "todo-write", CancellationToken(), "call-complete"),
    )
    assert completed.is_error is False
    restored = tool.execute(
        {"operation": "restore", "targetTitle": "提交报告（角色更新）"},
        ToolContext("role-1", "role:role-1", "todo-write", CancellationToken(), "call-restore"),
    )
    assert restored.is_error is False
    assert todos.get(item["id"])["status"] == "scheduled"

    monkeypatch.setattr(main, "todo_store", todos)
    client = TestClient(main.app)
    edited = client.put(f"/api/todos/{item['id']}", json={"title": "提交实验报告（终稿）"})
    assert edited.status_code == 200
    assert edited.json()["title"] == "提交实验报告（终稿）"
    assert todos.get(item["id"])["title"] == "提交实验报告（终稿）"

    invalid = client.post("/api/todos", json={"title": "冲突", "reminderAt": "2026-10-09T10:00:00+08:00", "reminderDate": "2026-10-09"})
    assert invalid.status_code == 422
    invalid_title = client.put(f"/api/todos/{item['id']}", json={"title": None})
    assert invalid_title.status_code == 422


def test_web_created_todo_has_no_chat_source(tmp_path, monkeypatch):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    application = TodoApplicationService(todos, sessions)
    monkeypatch.setattr(main, "todo_store", todos)
    monkeypatch.setattr(main, "todo_application", application)

    created = TestClient(main.app).post("/api/todos", json={"title": "网页创建的待办"})

    assert created.status_code == 201
    assert created.json()["sourceRoleId"] is None
    assert created.json()["sourceSessionKey"] is None
    assert created.json()["sourceMessageId"] is None


def test_tool_refuses_unqualified_and_missing_source_writes(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    sessions.open_role_session("role-1")
    application = TodoApplicationService(todos, sessions)
    tool = TodoWriteTool(application)
    no_source = tool.execute(
        {"operation": "create", "title": "没有来源"},
        ToolContext("role-1", "role:role-1", "missing-run", CancellationToken(), "call-1"),
    )
    assert no_source.is_error is True
    assert todos.list() == []

    _, _, assistant = sessions.create_run_with_messages(_run("turn-1"), "我下周可能要搬家")
    sessions.finish_run("turn-1", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    assert todos.list() == []


def test_tool_does_not_modify_multiple_matching_todos(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "提交报告"})
    todos.create({"title": "提交报告"})
    sessions = _sessions(tmp_path)
    sessions.open_role_session("role-1")
    sessions.create_run_with_messages(_run("ambiguous"), "取消提交报告")
    tool = TodoWriteTool(TodoApplicationService(todos, sessions))

    result = tool.execute(
        {"operation": "cancel", "targetTitle": "提交报告"},
        ToolContext("role-1", "role:role-1", "ambiguous", CancellationToken(), "call-cancel"),
    )

    assert result.is_error is True
    assert [todo["status"] for todo in todos.list()] == ["inbox", "inbox"]
    diagnostic, = todos.operation_diagnostics()
    assert diagnostic["result"] == "needs_review"


def test_plugin_manifest_registers_optional_search_and_all_role_write_tools(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    registry = PluginRegistry()
    register_todo_agent_plugin(registry)

    async def resolve():
        return await CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1", session_key="role:role-1", run_id="run-1",
            enabled_tools={"todo.search", "todo.write", "todo.reminder_control"},
            enabled_plugin_ids={"todo"},
            granted_capabilities={"todo.write"},
            host_services={"todo.write": TodoApplicationService(todos, sessions)},
        )

    capabilities = asyncio.run(resolve())
    try:
        assert capabilities.get("todo.search") is not None
        assert capabilities.get("todo.write") is not None
        assert capabilities.get("todo.reminder_control") is not None
        assert capabilities.definition("todo.search").risk == "read_only"
        assert capabilities.definition("todo.write").risk == "mutating"
    finally:
        asyncio.run(capabilities.close())


def test_reminder_role_qualification_takes_effect_the_next_day(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    assert todos.reminder_role_ids(on_date=date(2026, 10, 8), available_role_ids=["role-1", "role-2"]) == []
    assert todos.reminder_role_ids(on_date=date(2026, 10, 9), available_role_ids=["role-1", "role-2"]) == ["role-1"]
    assert todos.reminder_role_ids(on_date=date(2026, 10, 9), available_role_ids=["role-2"]) == []
    todos.set_reminder_role("role-1", False, effective_date=date(2026, 10, 10))
    assert todos.reminder_role_ids(on_date=date(2026, 10, 9), available_role_ids=["role-1"]) == ["role-1"]
    assert todos.reminder_role_ids(on_date=date(2026, 10, 10), available_role_ids=["role-1"]) == []
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 11))
    assert todos.reminder_role_ids(on_date=date(2026, 10, 10), available_role_ids=["role-1"]) == []
    assert todos.reminder_role_ids(on_date=date(2026, 10, 11), available_role_ids=["role-1"]) == ["role-1"]


def test_one_reminder_role_serves_the_whole_day_and_reselects_when_unavailable(tmp_path):
    todos, _sessions_value, plugin = _plugin(tmp_path, roles=("role-1", "role-2"))
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    todos.set_reminder_role("role-2", True, effective_date=date(2026, 10, 9))
    todos.create({"title": "交报告", "dueDate": "2026-10-09", "reminderAt": "2026-10-09T12:00:00+08:00", "timezone": "Asia/Shanghai"})

    moment = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    plugin.schedule_reminders_once(now=moment)
    selected = todos.reminder_day(date(2026, 10, 9))["roleId"]
    assert selected in {"role-1", "role-2"}

    plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 4, 30, tzinfo=timezone.utc))
    messages = [message for role in ("role-1", "role-2") for message in _sessions_value.list_messages(f"role:{role}")]
    proactive = [message for message in messages if message.messageType == "proactive_reminder"]
    assert len(proactive) == 2
    assert {message.sessionKey for message in proactive} == {f"role:{selected}"}

    remaining = {role for role in ("role-1", "role-2") if role != selected}
    plugin.available_roles = lambda: sorted(remaining)
    plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc))
    assert todos.reminder_day(date(2026, 10, 9))["roleId"] in remaining


def test_disabling_selected_reminder_role_reselects_today_or_stops(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    today = date(2026, 10, 9)
    roles = ["role-1", "role-2"]
    for role_id in roles:
        todos.set_reminder_role(role_id, True, effective_date=today)

    selected = todos.ensure_reminder_day(today, roles)
    assert selected in roles
    todos.set_reminder_role(selected, False, effective_date=today)

    replacement = todos.ensure_reminder_day(today, roles)
    assert replacement in roles
    assert replacement != selected

    todos.set_reminder_role(replacement, False, effective_date=today)
    assert todos.ensure_reminder_day(today, roles) is None


def test_scheduler_does_not_send_when_no_reminder_role_is_enabled(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    today = date(2026, 10, 9)
    todos.set_reminder_role("role-1", True, effective_date=today)
    todos.set_reminder_role("role-1", False, effective_date=today)
    todos.create({"title": "吃药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})

    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc)) == []
    assert [message for message in sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"] == []
    assert todos.reminder_day(today)["roleId"] is None
    assert todos.list_reminders()[0]["status"] == "failed"


def test_delivered_reminders_remain_deduplicated_after_store_and_scheduler_restart(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    today = date(2026, 10, 9)
    todos.set_reminder_role("role-1", True, effective_date=today)
    todos.create({"title": "吃药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})
    moment = datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc)

    assert len(plugin.schedule_reminders_once(now=moment)) == 2
    selected_role = todos.reminder_day(today)["roleId"]

    restarted_todos = TodoStore(tmp_path / "todos.db")
    restarted_sessions = _sessions(tmp_path)
    restarted_plugin = TodoPlugin(
        restarted_todos,
        restarted_sessions,
        reminder_sender=lambda role_id, _event_id, kind, payload: f"{role_id}:{kind}:" + "、".join(str(item["title"]) for item in payload),
        available_roles=lambda: ["role-1"],
    )

    assert restarted_todos.reminder_day(today)["roleId"] == selected_role
    assert restarted_plugin.schedule_reminders_once(now=moment) == []
    proactive = [message for message in restarted_sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"]
    assert len(proactive) == 2


def test_daily_summary_is_sent_once_after_four_and_empty_digest_is_not_sent(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    todos.create({"title": "交报告", "dueDate": "2026-10-09", "timezone": "Asia/Shanghai"})

    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 8, 19, 59, tzinfo=timezone.utc)) == []
    delivered = plugin.schedule_reminders_once(now=datetime(2026, 10, 8, 20, 1, tzinfo=timezone.utc))
    assert len(delivered) == 1
    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 8, 20, 30, tzinfo=timezone.utc)) == []
    assert [message.content for message in sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"] == ["role-1:daily:交报告"]


def test_daily_summary_passes_date_only_reminder_as_a_fact(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    item = todos.create({"title": "整理文件", "reminderDate": "2026-10-09", "timezone": "Asia/Shanghai"})

    payload = todos.daily_summary_payload(date(2026, 10, 9))

    assert payload == [{
        "id": item["id"],
        "title": "整理文件",
        "description": "",
        "dueDate": None,
        "dueAt": None,
        "reminderAt": None,
        "reminderDate": "2026-10-09",
        "timezone": "Asia/Shanghai",
    }]


def test_daily_summary_and_pause_dates_use_owner_timezone(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.update_settings(timezone_name="Asia/Shanghai")
    item = todos.create({
        "title": "跨时区提交",
        "dueAt": "2026-10-09T22:30:00-07:00",
        "reminderAt": "2026-10-09T22:30:00-07:00",
        "timezone": "America/Los_Angeles",
    })

    assert all(todo["id"] != item["id"] for todo in todos.todos_for_daily_summary(date(2026, 10, 9)))
    assert any(todo["id"] == item["id"] for todo in todos.todos_for_daily_summary(date(2026, 10, 10)))
    due = todos.schedule_due_reminders(
        now=datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc),
        skip_dates={date(2026, 10, 9)},
    )
    assert len(due) == 1
    assert due[0]["status"] == "pending"


def test_exact_reminder_waits_until_owner_local_time_and_uses_the_same_day_role(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    todos.create({"title": "吃药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})

    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 8, 19, 59, tzinfo=timezone.utc)) == []
    assert all(item["kind"] == "daily" for item in plugin.schedule_reminders_once(now=datetime(2026, 10, 8, 20, 1, tzinfo=timezone.utc)))
    delivered = plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc))
    assert len(delivered) == 1
    assert delivered[0]["kind"] == "exact"
    assert [message.content for message in sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"] == ["role-1:daily:吃药", "role-1:exact:吃药"]


def test_completed_todo_does_not_receive_a_pending_exact_reminder(tmp_path):
    todos, _sessions_value, plugin = _plugin(tmp_path)
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    item = todos.create({"title": "交文件", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})
    todos.set_status(item["id"], "completed")

    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)) == []


def test_restoring_scheduled_todo_preserves_its_scheduled_status(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    item = todos.create({"title": "按时服药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})
    todos.set_status(item["id"], "completed")
    application = TodoApplicationService(todos, sessions)

    restored = application.set_status(item["id"], "scheduled")

    assert restored["status"] == "scheduled"


def test_pausing_today_skips_pending_and_resuming_does_not_backfill(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    todos.create({"title": "交报告", "dueDate": "2026-10-09", "reminderAt": "2026-10-09T12:00:00+08:00", "timezone": "Asia/Shanghai"})
    todos.set_reminders_paused(date(2026, 10, 9))

    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)) == []
    todos.set_reminders_paused(None)
    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)) == []
    assert todos.get(todos.list()[0]["id"])["reminderAt"] == "2026-10-09T04:00:00+00:00"
    assert [message for message in sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"] == []
    with _sqlite(todos.database_path) as connection:
        assert connection.execute("SELECT status FROM todo_reminders").fetchone()[0] == "skipped"


def test_failure_is_persisted_with_retry_backoff_and_never_repeats_a_delivered_message(tmp_path):
    todos, sessions, plugin = _plugin(tmp_path)
    todos.set_reminder_role("role-1", True, effective_date=date(2026, 10, 9))
    todos.create({"title": "吃药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})
    calls = []

    def flaky(role_id, event_id, kind, payload):
        calls.append(event_id)
        if len(calls) <= 2:
            raise RuntimeError("temporary failure")
        return "吃药时间到了"

    plugin.reminder_sender = flaky
    assert all(item["kind"] == "daily" for item in plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc)))
    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 2, 1, 30, tzinfo=timezone.utc)) == []
    with _sqlite(todos.database_path) as connection:
        reminder_row = connection.execute("SELECT status,attempts,next_attempt_at FROM todo_reminders").fetchone()
        digest_row = connection.execute("SELECT digest_status,attempts,next_attempt_at FROM todo_reminder_days").fetchone()
    assert reminder_row[0] == "failed"
    assert int(reminder_row[1]) == 1
    assert reminder_row[2]
    assert digest_row[0] == "failed"
    assert int(digest_row[1]) == 1
    assert digest_row[2]

    delivered = plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 4, 1, tzinfo=timezone.utc))
    assert len(delivered) == 2
    assert plugin.schedule_reminders_once(now=datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)) == []
    assert [message.content for message in sessions.list_messages("role:role-1") if message.messageType == "proactive_reminder"] == ["吃药时间到了", "吃药时间到了"]


def test_manual_retry_clears_backoff_and_makes_failed_reminder_due_immediately(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "吃药", "reminderAt": "2026-10-09T10:00:00+08:00", "timezone": "Asia/Shanghai"})
    moment = datetime(2026, 10, 9, 2, 1, tzinfo=timezone.utc)
    reminder = todos.schedule_due_reminders(now=moment)[0]
    todos.finish_role_reminder(reminder["id"], error="temporary failure", retry_base=moment)

    assert todos.pending_role_reminders(now=moment) == []
    assert todos.retry_reminder(reminder["id"]) is True
    assert [item["id"] for item in todos.pending_role_reminders(now=moment)] == [reminder["id"]]


def test_proactive_message_is_global_once_and_not_provider_or_memory_context(tmp_path):
    _todos, sessions, _plugin_value = _plugin(tmp_path, roles=("role-1", "role-2"))
    sessions.open_role_session("role-2")
    first = sessions.append_proactive_message_once("role:role-1", "早安", event_id="daily:2026-10-09", metadata={"kind": "daily"})
    again = sessions.append_proactive_message_once("role:role-2", "早安", event_id="daily:2026-10-09", metadata={"kind": "daily"})

    assert again.id == first.id
    assert again.sessionKey == "role:role-1"
    assert [message.id for message in sessions.list_messages("role:role-2")] == []
    assert sessions.runtime_messages("role:role-1") == []
    assert sessions.context_messages("role:role-1") == []


def test_session_manager_shows_proactive_messages(tmp_path):
    from backend.app.role_store import RoleStore
    from backend.app.session_manager import SessionManager

    sessions = _sessions(tmp_path)
    roles_root = tmp_path / "roles"
    roles_root.mkdir()
    roles = RoleStore(roles_root)
    from backend.app.models import RoleInput, RoleProfile

    role = roles.create(RoleInput(name="爱丽丝", profile=RoleProfile(profile="女仆", personality="安静")))
    manager = SessionManager(roles, sessions)
    manager.open_role_session(role.id)
    sessions.append_proactive_message_once(f"role:{role.id}", "该交报告了", event_id="daily:2026-10-09")

    response = manager.open_role_session(role.id)

    assert [message.messageType for message in response.messages] == ["proactive_reminder"]
    assert response.messages[0].content == "该交报告了"


def test_role_reminder_generation_uses_personality_facts_and_read_only_related_memory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.role_store import RoleStore

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(
        name="爱丽丝",
        profile=RoleProfile(profile="贴心女仆", personality="温柔而细心"),
    ))
    calls = {}

    class Memory:
        async def query(self, query):
            calls["query"] = query
            return SimpleNamespace(text_block="主人习惯在出门前确认药盒。")

    class Adapter:
        async def complete_messages(self, messages, configuration, *, max_tokens=None):
            calls["messages"] = messages
            calls["configuration"] = configuration
            calls["max_tokens"] = max_tokens
            return "主人，出门前记得带上药盒哦。"

    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "model_configuration_store", SimpleNamespace(get=lambda _identifier=None: "configured"))
    monkeypatch.setattr(main, "model_adapter", Adapter())
    monkeypatch.setattr(main, "_current_memory_engine", lambda: Memory())

    result = main._generate_todo_reminder(role.id, "exact:todo-1", "exact", [{"title": "取药", "description": ""}])

    assert result == "主人，出门前记得带上药盒哦。"
    assert calls["query"].effect == "read_only"
    assert calls["query"].scope.role_id == role.id
    assert "温柔而细心" in calls["messages"][0]["content"]
    assert "主人习惯在出门前确认药盒" in calls["messages"][0]["content"]
    assert '"title": "取药"' in calls["messages"][0]["content"]


def test_reminder_control_only_changes_the_current_role_and_takes_effect_tomorrow(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    sessions.open_role_session("role-1")
    _, _, assistant = sessions.create_run_with_messages(_run("control"), "以后你也负责提醒任务")
    sessions.finish_run("control", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    application = TodoApplicationService(todos, sessions)
    tool = TodoReminderControlTool(application)
    context = ToolContext("role-1", "role:role-1", "control", CancellationToken(), "call-join")

    result = tool.execute({"action": "join"}, context)

    assert result.is_error is False
    today = datetime.now(timezone.utc).astimezone(todos_zone(todos)).date()
    assert result.details["roleId"] == "role-1"
    assert result.details["effectiveDate"] == (today + timedelta(days=1)).isoformat()
    assert todos.reminder_role_ids(on_date=today, available_role_ids=["role-1"]) == []
    assert todos.reminder_role_ids(on_date=today + timedelta(days=1), available_role_ids=["role-1"]) == ["role-1"]

    _, _, assistant = sessions.create_run_with_messages(_run("pause-today"), "停止今天的提醒")
    sessions.finish_run("pause-today", status="completed", assistant_message_id=assistant.id, assistant_content="好的", turn_count=1)
    paused = tool.execute(
        {"action": "pause_today"},
        ToolContext("role-1", "role:role-1", "pause-today", CancellationToken(), "call-pause"),
    )
    assert paused.is_error is False
    assert todos.reminders_paused(today) is True

    _, _, assistant = sessions.create_run_with_messages(_run("resume-today"), "继续今天的提醒")
    sessions.finish_run("resume-today", status="completed", assistant_message_id=assistant.id, assistant_content="好", turn_count=1)
    resumed = tool.execute(
        {"action": "resume_today"},
        ToolContext("role-1", "role:role-1", "resume-today", CancellationToken(), "call-resume"),
    )
    assert resumed.is_error is False
    assert todos.reminders_paused(today) is False


def test_web_settings_expose_reminder_roles_and_today_pause(tmp_path, monkeypatch):
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.role_store import RoleStore

    todos = TodoStore(tmp_path / "todos.db")
    sessions = _sessions(tmp_path)
    plugin = TodoPlugin(todos, sessions)
    application = TodoApplicationService(todos, sessions)
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="爱丽丝", profile=RoleProfile(profile="女仆")))
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "todo_store", todos)
    monkeypatch.setattr(main, "todo_application", application)
    monkeypatch.setattr(main, "todo_plugin", plugin)
    client = TestClient(main.app)

    settings = client.get("/api/todos/settings")
    assert settings.status_code == 200
    assert settings.json()["remindersEnabled"] is True
    paused = client.put("/api/todos/settings", json={"remindersEnabled": False})
    assert paused.status_code == 200
    assert paused.json()["enabled"] is True
    assert paused.json()["remindersEnabled"] is False
    resumed = client.put("/api/todos/settings", json={"remindersEnabled": True})
    assert resumed.status_code == 200
    assert resumed.json()["remindersEnabled"] is True

    roles = client.get("/api/todos/reminder-roles")
    assert roles.status_code == 200
    assert roles.json()["roles"] == [{
        "id": role.id,
        "name": "爱丽丝",
        "roleId": role.id,
        "enabled": False,
        "activeToday": False,
        "effectiveDate": datetime.now(timezone.utc).astimezone(todos_zone(todos)).date().isoformat(),
    }]
    enabled = client.put(f"/api/todos/reminder-roles/{role.id}", json={"enabled": True})
    assert enabled.status_code == 200
    tomorrow = datetime.now(timezone.utc).astimezone(todos_zone(todos)).date() + timedelta(days=1)
    assert enabled.json() == {"roleId": role.id, "enabled": True, "effectiveDate": tomorrow.isoformat()}
    assert client.get("/api/todos/reminder-roles").json()["roles"][0]["enabled"] is True
    assert client.get("/api/todos/reminder-roles").json()["roles"][0]["activeToday"] is False
    disabled = client.put(f"/api/todos/reminder-roles/{role.id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["effectiveDate"] == datetime.now(timezone.utc).astimezone(todos_zone(todos)).date().isoformat()
    disabled_role = client.get("/api/todos/reminder-roles").json()["roles"][0]
    assert disabled_role["enabled"] is False
    assert disabled_role["activeToday"] is False

    today = client.get("/api/todos/reminders/today")
    assert today.status_code == 200
    assert today.json()["paused"] is False
    paused_today = client.put("/api/todos/reminders/today?paused=true")
    assert paused_today.status_code == 200
    assert paused_today.json()["paused"] is True
    assert client.get("/api/todos/reminders/today").json()["paused"] is True


def test_todo_search_reads_the_owner_list_without_writing(tmp_path):
    todos = TodoStore(tmp_path / "todos.db")
    todos.create({"title": "交实验报告"})
    registry = PluginRegistry()
    register_todo_agent_plugin(registry)

    async def resolve():
        return await CapabilityRegistry(plugins=registry).resolve_async(
            role_id="role-1", session_key="role:role-1", run_id="run-1",
            enabled_tools={"todo.search"}, enabled_plugin_ids={"todo"},
            granted_capabilities={"todo.write"},
            host_services={"todo.write": TodoApplicationService(todos, _sessions(tmp_path))},
        )

    capabilities = asyncio.run(resolve())
    try:
        tool = capabilities.get("todo.search")
        assert tool is not None
        assert capabilities.definition("todo.search").risk == "read_only"
        result = asyncio.run(tool.execute({"query": "实验报告"}, ToolContext("role-1", "role:role-1", "run-1", CancellationToken())))
        assert "交实验报告" in result.content
        assert len(todos.list()) == 1
    finally:
        asyncio.run(capabilities.close())


def _sqlite(path):
    import sqlite3

    return sqlite3.connect(path)


def todos_zone(todos):
    from zoneinfo import ZoneInfo

    return ZoneInfo(str(todos.settings()["timezone"]))
