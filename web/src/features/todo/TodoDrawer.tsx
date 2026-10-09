import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import type { TodoDiagnostic, TodoItem, TodoReminder, TodoSettings, TodoStatus } from "../../api/types";
import { Dialog } from "../../ui/Dialog";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";

type FormValue = { title: string; description: string; dueDate: string; reminderDate: string; reminderTime: string };
const emptyForm = (): FormValue => ({ title: "", description: "", dueDate: "", reminderDate: "", reminderTime: "" });
const states: { id: TodoStatus | "all"; label: string }[] = [
  { id: "all", label: "全部" }, { id: "inbox", label: "收件箱" }, { id: "scheduled", label: "已安排" },
  { id: "completed", label: "已完成" }, { id: "cancelled", label: "已取消" },
];

function zonedParts(iso: string | null, timezone: string): { date: string; time: string } {
  if (!iso) return { date: "", time: "" };
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return { date: `${values.year}-${values.month}-${values.day}`, time: `${values.hour}:${values.minute}` };
}

function dateLabel(todo: TodoItem): string {
  if (todo.dueAt) return new Date(todo.dueAt).toLocaleString();
  if (todo.dueDate) return todo.dueDate;
  return "未安排日期";
}

export function TodoDrawer({ onClose, onOpenSource }: { onClose: () => void; onOpenSource: (roleId: string, sessionKey: string, messageId: string) => void }) {
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [reminders, setReminders] = useState<TodoReminder[]>([]);
  const [diagnostics, setDiagnostics] = useState<TodoDiagnostic[]>([]);
  const [settings, setSettings] = useState<TodoSettings>({ enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" });
  const [filter, setFilter] = useState<TodoStatus | "all">("all");
  const [editing, setEditing] = useState<TodoItem | null | "new">(null);
  const [form, setForm] = useState<FormValue>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [focusedTodoId, setFocusedTodoId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [settingsValue, todoValue, reminderValue, diagnosticValue] = await Promise.all([
        api<TodoSettings>("/api/todos/settings"),
        api<{ todos: TodoItem[] }>(`/api/todos${filter === "all" ? "" : `?status=${filter}`}`),
        api<{ reminders: TodoReminder[] }>("/api/todos/reminders"),
        api<{ items: TodoDiagnostic[] }>("/api/todos/diagnostics"),
      ]);
      setSettings(settingsValue);
      setTodos(todoValue.todos);
      setReminders(reminderValue.reminders);
      setDiagnostics(diagnosticValue.items);
      setError("");
    } catch (cause) {
      setError(errorMessage(cause, "待办加载失败"));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const startCreate = () => {
    setEditing("new");
    setForm(emptyForm());
  };

  const startEdit = (todo: TodoItem) => {
    const parts = zonedParts(todo.reminderAt, settings.timezone);
    setEditing(todo);
    setForm({
      title: todo.title,
      description: todo.description,
      dueDate: todo.dueDate ?? "",
      reminderDate: todo.reminderDate ?? parts.date,
      reminderTime: parts.time,
    });
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.title.trim()) return;
    const payload = {
      title: form.title.trim(),
      description: form.description,
      dueDate: form.dueDate || null,
      reminderDate: form.reminderTime ? null : (form.reminderDate || null),
      reminderAt: form.reminderDate && form.reminderTime ? `${form.reminderDate}T${form.reminderTime}:00` : null,
      timezone: settings.timezone,
    };
    try {
      if (editing === "new") await api<TodoItem>("/api/todos", { method: "POST", body: JSON.stringify(payload) });
      else if (editing) await api<TodoItem>(`/api/todos/${encodeURIComponent(editing.id)}`, { method: "PUT", body: JSON.stringify(payload) });
      setEditing(null);
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause, "待办保存失败"));
    }
  };

  const setStatus = async (todo: TodoItem, status: TodoStatus) => {
    try {
      await api<TodoItem>(`/api/todos/${encodeURIComponent(todo.id)}/status`, { method: "PUT", body: JSON.stringify({ status }) });
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause, "待办状态更新失败"));
    }
  };

  const openReminder = async (reminder: TodoReminder) => {
    try {
      await api(`/api/todos/reminders/${encodeURIComponent(reminder.id)}/read`, { method: "POST" });
      setFilter(reminder.todoStatus);
      setFocusedTodoId(reminder.todoId);
      await refresh();
      window.setTimeout(() => document.getElementById(`todo-${reminder.todoId}`)?.scrollIntoView?.({ behavior: "smooth", block: "center" }), 0);
    } catch (cause) {
      setError(errorMessage(cause, "提醒状态更新失败"));
    }
  };

  const sourceClick = (todo: TodoItem) => {
    if (todo.sourceAvailable && todo.sourceRoleId && todo.sourceSessionKey && todo.sourceMessageId) {
      onOpenSource(todo.sourceRoleId, todo.sourceSessionKey, todo.sourceMessageId);
    }
  };

  return <Dialog title="待办" variant="drawer" onClose={onClose}>
    <div className="todo-drawer">
      <section className="todo-reminders" aria-label="提醒收件箱">
        <div className="todo-section-heading"><h3>提醒</h3><span>{reminders.filter((reminder) => !reminder.readAt).length}</span></div>
        {!settings.remindersEnabled && <p className="todo-muted">提醒已暂停，待办清单仍可继续使用。</p>}
        {reminders.length ? <ul className="todo-reminder-list">{reminders.map((reminder) => <li key={reminder.id} className={!reminder.readAt ? "unread" : ""}>
          <button type="button" className="todo-reminder-open" onClick={() => { void openReminder(reminder); }}><strong>{reminder.title}</strong><time>{reminder.dueKey.includes(":date:") ? reminder.dueKey.split(":date:")[1] : new Date(reminder.dueAt).toLocaleString()}</time></button>
          {reminder.delayed && <small className="todo-delayed">延迟提醒</small>}
          {reminder.status === "pending" && <small className="todo-muted">提醒待投递</small>}
          {reminder.status === "failed" && <small className="todo-error">{reminder.lastError ?? "提醒投递失败"}</small>}
          {reminder.status === "failed" && <button type="button" className="todo-action" onClick={() => void api(`/api/todos/reminders/${encodeURIComponent(reminder.id)}/retry`, { method: "POST" }).then(refresh).catch((cause) => setError(errorMessage(cause, "提醒重试失败")))}>重试提醒</button>}
        </li>)}</ul> : <p className="todo-muted">暂无提醒</p>}
      </section>

      {diagnostics.length > 0 && <section className="todo-diagnostics" aria-label="待办整理状态">
        <div className="todo-section-heading"><h3>需要处理</h3></div>
        {diagnostics.map((item) => <div className="todo-diagnostic" key={item.eventId ?? item.id ?? `${item.operation}-${item.todoId}`}>
          <span>{item.error ?? "待办操作需要确认"}</span>
        </div>)}
      </section>}

      <section className="todo-list-section" aria-label="待办清单">
        <div className="todo-section-heading"><h3>清单</h3><button type="button" className="todo-add" aria-label="新建待办" title="新建待办" onClick={startCreate}><Icon name="plus" size={18} /></button></div>
        <div className="todo-filters" role="tablist" aria-label="待办状态">
          {states.map((state) => <button type="button" role="tab" aria-selected={filter === state.id} key={state.id} onClick={() => setFilter(state.id)}>{state.label}</button>)}
        </div>
        {editing !== null && <form className="todo-form" onSubmit={(event) => void save(event)}>
          <div className="todo-form-heading"><strong>{editing === "new" ? "新建待办" : "编辑待办"}</strong><IconButton icon="close" label="取消编辑" onClick={() => setEditing(null)} /></div>
          <label className="field"><span>标题</span><input autoFocus maxLength={200} required value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
          <label className="field"><span>说明</span><textarea rows={2} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
          <div className="todo-date-fields">
            <label className="field"><span>截止日期</span><input type="date" value={form.dueDate} onChange={(event) => setForm({ ...form, dueDate: event.target.value })} /></label>
            <label className="field"><span>提醒日期</span><input type="date" value={form.reminderDate} onChange={(event) => setForm({ ...form, reminderDate: event.target.value })} /></label>
            <label className="field"><span>提醒时刻</span><input type="time" value={form.reminderTime} disabled={!form.reminderDate} onChange={(event) => setForm({ ...form, reminderTime: event.target.value })} /></label>
          </div>
          <div className="form-actions"><button type="button" className="ghost" onClick={() => setEditing(null)}>取消</button><button type="submit" className="primary">保存</button></div>
        </form>}

        {loading ? <Placeholder loading>正在加载待办…</Placeholder>
          : todos.length ? <ul className="todo-items">{todos.map((todo) => <li id={`todo-${todo.id}`} className={`todo-item${focusedTodoId === todo.id ? " focused" : ""}`} key={todo.id}>
            <div className="todo-item-heading"><strong>{todo.title}</strong><span className={`todo-status ${todo.status}`}>{states.find((state) => state.id === todo.status)?.label}</span></div>
            {todo.description && <p>{todo.description}</p>}
            <time>{dateLabel(todo)}</time>
            {todo.sourceMessageId && <button type="button" className="todo-source" disabled={!todo.sourceAvailable} onClick={() => sourceClick(todo)}>{todo.sourceAvailable ? "查看来源" : "来源角色已删除"}</button>}
            <div className="todo-actions">
              {(todo.status === "inbox" || todo.status === "scheduled") && <>
                <button type="button" className="todo-action" onClick={() => void setStatus(todo, "completed")}>完成</button>
                <button type="button" className="todo-action" onClick={() => void setStatus(todo, "cancelled")}>取消</button>
                <button type="button" className="todo-icon-action" aria-label={`编辑${todo.title}`} title="编辑" onClick={() => startEdit(todo)}><Icon name="edit" size={15} /></button>
              </>}
              {(todo.status === "completed" || todo.status === "cancelled") && <button type="button" className="todo-action" onClick={() => void setStatus(todo, todo.dueDate || todo.dueAt || todo.reminderAt || todo.reminderDate ? "scheduled" : "inbox")}>恢复</button>}
            </div>
          </li>)}</ul> : <p className="todo-muted">此状态下没有待办</p>}
      </section>
      {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    </div>
  </Dialog>;
}
