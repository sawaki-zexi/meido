import { useEffect, useRef, useState, type FormEvent } from "react";
import type { TodoItem, TodoReminder } from "../../api/types";
import { Dialog } from "../../ui/Dialog";
import { Icon } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { dayLabel, dateTimeLabel, filters, groupTodos, isActive, matchesFilter, statusLabels, todoTiming, zonedParts, type TodoFilter } from "./presentation";
import { useTodos, type TodoInput } from "./useTodos";
import "./todo.css";

type Props = { onClose: () => void; onOpenSource: (roleId: string, sessionKey: string, messageId: string) => void };
type FormValue = { title: string; description: string; dueDate: string; dueTime: string; reminderDate: string; reminderTime: string };
const emptyForm = (): FormValue => ({ title: "", description: "", dueDate: "", dueTime: "", reminderDate: "", reminderTime: "" });
function formFor(todo: TodoItem, timezone: string): FormValue {
  const due = todo.dueAt ? zonedParts(todo.dueAt, timezone) : { date: todo.dueDate ?? "", time: "" };
  const reminder = todo.reminderAt ? zonedParts(todo.reminderAt, timezone) : { date: todo.reminderDate ?? "", time: "" };
  return { title: todo.title, description: todo.description, dueDate: due.date, dueTime: due.time, reminderDate: reminder.date, reminderTime: reminder.time };
}

export function TodoDrawer({ onClose, onOpenSource }: Props) {
  const data = useTodos();
  const [tab, setTab] = useState<"list" | "reminders">("list");
  const [filter, setFilter] = useState<TodoFilter>("active");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<TodoItem | "new" | null>(null);
  const [form, setForm] = useState<FormValue>(emptyForm);
  const [formTimezone, setFormTimezone] = useState(data.settings.timezone);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const timezone = data.settings.timezone;
  const today = zonedParts(new Date().toISOString(), timezone).date;
  const term = query.trim().toLocaleLowerCase();
  const visible = data.todos.filter(todo => matchesFilter(todo, filter) && `${todo.title} ${todo.description}`.toLocaleLowerCase().includes(term));
  const groups = groupTodos(visible, today, timezone);
  const unread = data.reminders.filter(reminder => !reminder.readAt).length;
  const activeCount = data.todos.filter(isActive).length;
  const readOnly = editing !== null && editing !== "new" && !isActive(editing);

  useEffect(() => {
    if (!focusedId || editing || tab !== "list") return;
    const row = document.getElementById(`todo-${focusedId}`);
    row?.scrollIntoView?.({ block: "nearest" });
    row?.querySelector<HTMLButtonElement>(".todo-row-open")?.focus();
  }, [focusedId, editing, tab]);

  const startEdit = (todo: TodoItem | "new") => {
    setEditing(todo);
    setFormTimezone(timezone);
    setForm(todo === "new" ? emptyForm() : formFor(todo, timezone));
  };
  const closeEditor = () => { setEditing(null); window.requestAnimationFrame(() => headingRef.current?.focus()); };
  const selectTab = (next: "list" | "reminders") => { setFocusedId(null); setTab(next); };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.title.trim() || !editing || readOnly) return;
    const original = editing === "new" ? emptyForm() : formFor(editing, formTimezone);
    const dueUnchanged = editing !== "new" && original.dueDate === form.dueDate && original.dueTime === form.dueTime;
    const reminderUnchanged = editing !== "new" && original.reminderDate === form.reminderDate && original.reminderTime === form.reminderTime;
    const payload: TodoInput = {
      title: form.title.trim(), description: form.description,
      dueDate: dueUnchanged ? editing.dueDate : form.dueTime ? null : form.dueDate || null,
      dueAt: dueUnchanged ? editing.dueAt : form.dueDate && form.dueTime ? `${form.dueDate}T${form.dueTime}:00` : null,
      reminderDate: reminderUnchanged ? editing.reminderDate : form.reminderTime ? null : form.reminderDate || null,
      reminderAt: reminderUnchanged ? editing.reminderAt : form.reminderDate && form.reminderTime ? `${form.reminderDate}T${form.reminderTime}:00` : null,
      timezone: formTimezone,
    };
    if (await data.save(payload, editing === "new" ? undefined : editing.id)) {
      setFilter(editing === "new" || isActive(editing) ? "active" : editing.status as TodoFilter); setQuery(""); setTab("list"); closeEditor();
    }
  };
  const openReminder = async (reminder: TodoReminder) => {
    if (await data.readReminder(reminder)) { setTab("list"); setFilter("all"); setQuery(""); setFocusedId(reminder.todoId); }
  };
  const restore = (todo: TodoItem) => data.setStatus(todo, todo.dueDate || todo.dueAt || todo.reminderDate || todo.reminderAt ? "scheduled" : "inbox");
  const sourceClick = (todo: TodoItem) => {
    if (todo.sourceAvailable && todo.sourceRoleId && todo.sourceSessionKey && todo.sourceMessageId) onOpenSource(todo.sourceRoleId, todo.sourceSessionKey, todo.sourceMessageId);
  };

  return <Dialog title="待办" variant="page" modalFocus onClose={onClose}>
    <div className="todo-workspace">
      {data.error && <Notice onDismiss={data.dismissError}>{data.error}</Notice>}
      {editing ? <form className="todo-editor" onSubmit={event => void save(event)}>
        <button type="button" className="todo-back" disabled={data.busy} onClick={closeEditor}><Icon name="back" size={16} />返回清单</button>
        <div className="todo-editor-title"><h3>{editing === "new" ? "新建待办" : "待办详情"}</h3>{editing !== "new" && <span>{statusLabels[editing.status]}</span>}</div>
        <fieldset className="todo-editor-fields" disabled={data.busy || readOnly}>
        <label className="field"><span>标题</span><input autoFocus={!readOnly} required maxLength={200} value={form.title} onChange={event => setForm({ ...form, title: event.target.value })} /></label>
        <label className="field"><span>说明</span><textarea rows={4} maxLength={2000} placeholder="补充细节，或留给之后的自己" value={form.description} onChange={event => setForm({ ...form, description: event.target.value })} /></label>
        <div className="todo-editor-dates">
          <label className="field"><span>截止日期</span><input type="date" value={form.dueDate} onChange={event => setForm({ ...form, dueDate: event.target.value, dueTime: event.target.value ? form.dueTime : "" })} /></label>
          <label className="field"><span>截止时刻</span><input type="time" disabled={!form.dueDate} value={form.dueTime} onChange={event => setForm({ ...form, dueTime: event.target.value })} /></label>
          <label className="field"><span>提醒日期</span><input type="date" value={form.reminderDate} onChange={event => setForm({ ...form, reminderDate: event.target.value, reminderTime: event.target.value ? form.reminderTime : "" })} /></label>
          <label className="field"><span>提醒时刻</span><input type="time" disabled={!form.reminderDate} value={form.reminderTime} onChange={event => setForm({ ...form, reminderTime: event.target.value })} /></label>
        </div>
        </fieldset>
        <p className="todo-editor-hint">{formTimezone} · {readOnly ? "恢复待办后可以继续编辑。" : "留空日期可稍后安排；仅选提醒日期时不设置具体时刻。"}</p>
        {editing !== "new" && editing.sourceMessageId && <button type="button" className="todo-origin" disabled={!editing.sourceAvailable || data.busy} onClick={() => sourceClick(editing)}><Icon name="chat" size={15} />{editing.sourceAvailable ? "查看来源" : "来源角色已删除"}</button>}
        <div className="todo-editor-actions">
          {editing !== "new" && isActive(editing) && <button type="button" className="danger" disabled={data.busy} onClick={async () => { if (await data.setStatus(editing, "cancelled")) closeEditor(); }}>取消待办</button>}
          <span />
          <button type="button" className="ghost" disabled={data.busy} onClick={closeEditor}>{readOnly ? "关闭详情" : "取消"}</button>
          {readOnly ? <button type="button" className="primary" disabled={data.busy} onClick={async () => {
            if (await restore(editing)) { setFilter("active"); setQuery(""); setTab("list"); closeEditor(); }
          }}>{data.busy ? "恢复中…" : "恢复待办"}</button> : <button type="submit" className="primary" disabled={data.busy || !form.title.trim()}>{data.busy ? "保存中…" : "保存"}</button>}
        </div>
      </form> : <div className="todo-layout">
        <aside className="todo-navigation" aria-label="待办导航">
        <div className="todo-overview">
          <h3 ref={headingRef} tabIndex={-1}>{new Intl.DateTimeFormat("zh-CN", { timeZone: timezone, month: "long", day: "numeric", weekday: "long" }).format(new Date())}</h3>
          <p>{data.loaded ? `${activeCount} 件未完成` : "正在读取…"}</p>
        </div>
        <div className="todo-tabs">
          <div className="todo-tab-buttons" role="tablist" aria-label="待办面板" onKeyDown={event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? "list" : event.key === "End" ? "reminders" : tab === "list" ? "reminders" : "list";
            selectTab(next); document.getElementById(`todo-${next}-tab`)?.focus();
          }}>
          <button type="button" role="tab" id="todo-list-tab" aria-controls="todo-list-panel" aria-selected={tab === "list"} tabIndex={tab === "list" ? 0 : -1} onClick={() => selectTab("list")}>待办 <span>{activeCount}</span></button>
          <button type="button" role="tab" id="todo-reminders-tab" aria-controls="todo-reminders-panel" aria-selected={tab === "reminders"} tabIndex={tab === "reminders" ? 0 : -1} onClick={() => selectTab("reminders")}>提醒{unread + data.diagnostics.length > 0 && <span>{unread + data.diagnostics.length}</span>}</button>
          </div>
          <button type="button" className="todo-create" disabled={data.busy || !data.loaded} onClick={() => startEdit("new")}><Icon name="plus" size={16} />新建待办</button>
        </div>
        {tab === "list" && data.loaded && <div className="todo-filter-pills" role="group" aria-label="待办状态">{filters.map(item => <button type="button" key={item.id} aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>{item.label}<span>{data.todos.filter(todo => matchesFilter(todo, item.id)).length}</span></button>)}</div>}
        </aside>
        <div className="todo-content">
        {data.loading ? <Placeholder loading>正在加载待办…</Placeholder> : !data.loaded ? <div className="todo-empty"><Icon name="refresh" size={28} /><h3>暂时无法读取待办</h3><p>请重试，已有事项会在连接恢复后显示。</p><button type="button" className="primary" onClick={() => { data.dismissError(); void data.refresh(); }}>重新加载</button></div> : tab === "list" ? <div id="todo-list-panel" role="tabpanel" aria-labelledby="todo-list-tab">
          <div className="todo-list-tools">
            <h3>{filters.find(item => item.id === filter)?.label}事项</h3>
            <label className="todo-search"><Icon name="search" size={17} /><input type="search" aria-label="搜索待办" placeholder="搜索待办" value={query} onChange={event => setQuery(event.target.value)} /></label>
          </div>
          {groups.length ? <div className="todo-groups">{groups.map(group => <section className="todo-group" key={group.key} aria-label={group.label}>
            <div className="todo-group-heading"><h4>{group.label}</h4><span>{group.items.length}</span><i aria-hidden="true" /></div>
            <ul>{group.items.map(todo => <li id={`todo-${todo.id}`} className={`todo-row ${todo.status}${focusedId === todo.id ? " focused" : ""}`} key={todo.id}>
              {todo.status === "cancelled" ? <span className="todo-cancelled-mark" aria-label="已取消"><Icon name="close" size={14} /></span> : <input className="todo-check" type="checkbox" aria-label={`${todo.status === "completed" ? "恢复" : "完成"}${todo.title}`} checked={todo.status === "completed"} disabled={data.busy} onChange={() => void (todo.status === "completed" ? restore(todo) : data.setStatus(todo, "completed"))} />}
              <button type="button" className="todo-row-open" aria-label={`查看待办：${todo.title}`} disabled={data.busy} onClick={() => startEdit(todo)}>
                <strong>{todo.title}</strong>{todo.description && <span className="todo-row-description">{todo.description}</span>}
                {(todoTiming(todo, timezone, today) || todo.sourceMessageId) && <span className="todo-row-meta">{todoTiming(todo, timezone, today)}{todo.sourceMessageId && <span><Icon name="chat" size={12} />{todo.sourceAvailable ? "来自对话" : "来源已删除"}</span>}</span>}
              </button>
              {todo.status === "cancelled" ? <button type="button" className="todo-restore" disabled={data.busy} onClick={() => void restore(todo)}>恢复</button> : <span className="todo-row-arrow" aria-hidden="true"><Icon name="back" size={15} /></span>}
            </li>)}</ul>
          </section>)}</div> : <div className="todo-empty"><Icon name={term ? "search" : "todo"} size={32} /><h3>{term ? "没有找到相关待办" : filter === "active" ? "眼前的事，都告一段落" : "这里还没有待办"}</h3><p>{term ? "换个关键词，或切换待办状态看看。" : filter === "active" ? "想到什么，随时记下来。" : "事项变更后，会收在这里。"}</p>{!term && filter === "active" && <button type="button" className="primary" onClick={() => startEdit("new")}><Icon name="plus" size={16} />添加一件待办</button>}</div>}
        </div> : <section id="todo-reminders-panel" className="todo-inbox" role="tabpanel" aria-labelledby="todo-reminders-tab">
          <div className="todo-inbox-heading"><h4>提醒记录</h4><p>{data.settings.remindersEnabled ? "来自角色的提醒，和需要你留意的事项。" : "提醒已暂停，待办清单仍可继续使用。"}</p></div>
          {data.reminders.length ? <ul>{data.reminders.map(reminder => <li className={`todo-inbox-item${!reminder.readAt ? " unread" : ""}`} key={reminder.id}>
            <span className="todo-inbox-dot" aria-hidden="true" /><div><button type="button" className="todo-inbox-open" disabled={data.busy} onClick={() => void openReminder(reminder)}><strong>{reminder.title}</strong><time>{reminder.dueKey.includes(":date:") ? dayLabel(reminder.dueKey.split(":date:")[1], today) : dateTimeLabel(reminder.dueAt, timezone, today)}</time></button>
            <div className="todo-inbox-status">{!reminder.readAt && <span>未读</span>}{reminder.delayed && <span>延迟提醒</span>}{reminder.status === "pending" && <span>待投递</span>}{reminder.status === "delivered" && <span>已送达</span>}{reminder.status === "failed" && <><span className="todo-error">{reminder.lastError ?? "提醒投递失败"}</span><button type="button" disabled={data.busy} onClick={() => void data.retryReminder(reminder)}>重试提醒</button></>}</div></div>
          </li>)}</ul> : <div className="todo-empty"><Icon name="todo" size={32} /><h3>暂时没有提醒</h3><p>设置好提醒后，送达记录会显示在这里。</p></div>}
          {data.diagnostics.length > 0 && <section className="todo-inbox-diagnostics" aria-label="待办整理状态"><h4>需要处理</h4>{data.diagnostics.map((item, index) => <p key={item.eventId ?? item.id ?? index}>{item.error ?? "待办操作需要确认"}</p>)}</section>}
        </section>}
        </div>
      </div>}
    </div>
  </Dialog>;
}
