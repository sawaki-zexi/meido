import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { TodoDiagnostic, TodoItem, TodoReminder, TodoSettings, TodoStatus } from "../../api/types";

export type TodoInput = Pick<TodoItem, "title" | "description" | "dueDate" | "reminderDate" | "reminderAt" | "timezone"> & { dueAt?: string | null };

export function useTodos() {
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [reminders, setReminders] = useState<TodoReminder[]>([]);
  const [diagnostics, setDiagnostics] = useState<TodoDiagnostic[]>([]);
  const [settings, setSettings] = useState<TodoSettings>({ enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" });
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef(false);
  const writing = useRef(false);
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    try {
      const [configuration, list, inbox, issues] = await Promise.all([
        api<TodoSettings>("/api/todos/settings"), api<{ todos: TodoItem[] }>("/api/todos"),
        api<{ reminders: TodoReminder[] }>("/api/todos/reminders"), api<{ items: TodoDiagnostic[] }>("/api/todos/diagnostics"),
      ]);
      if (!active.current || request !== sequence.current) return;
      setSettings(configuration); setTodos(list.todos); setReminders(inbox.reminders); setDiagnostics(issues.items); setLoaded(true);
    } catch (cause) {
      if (active.current && request === sequence.current) setError(errorMessage(cause, "待办加载失败"));
    } finally {
      if (active.current && request === sequence.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    active.current = true;
    void refresh();
    const timer = window.setInterval(() => { if (!writing.current) void refresh(); }, 15000);
    return () => { active.current = false; ++sequence.current; window.clearInterval(timer); };
  }, [refresh]);

  const mutate = async (operation: () => Promise<void>, fallback: string) => {
    if (writing.current) return false;
    writing.current = true; ++sequence.current; setBusy(true); setError("");
    try {
      await operation();
      await refresh();
      return true;
    } catch (cause) {
      if (active.current) setError(errorMessage(cause, fallback));
      return false;
    } finally {
      writing.current = false;
      if (active.current) setBusy(false);
    }
  };

  const updateItem = (item: TodoItem) => {
    if (active.current) setTodos(current => current.some(todo => todo.id === item.id) ? current.map(todo => todo.id === item.id ? item : todo) : [...current, item]);
  };
  const save = (value: TodoInput, id?: string) => mutate(async () => {
    updateItem(await api<TodoItem>(id ? `/api/todos/${encodeURIComponent(id)}` : "/api/todos", { method: id ? "PUT" : "POST", body: JSON.stringify(value) }));
  }, "待办保存失败");
  const setStatus = (todo: TodoItem, status: TodoStatus) => mutate(async () => {
    updateItem(await api<TodoItem>(`/api/todos/${encodeURIComponent(todo.id)}/status`, { method: "PUT", body: JSON.stringify({ status }) }));
  }, "待办状态更新失败");
  const readReminder = (reminder: TodoReminder) => mutate(async () => {
    await api(`/api/todos/reminders/${encodeURIComponent(reminder.id)}/read`, { method: "POST" });
    if (active.current) setReminders(current => current.map(item => item.id === reminder.id ? { ...item, readAt: new Date().toISOString() } : item));
  }, "提醒状态更新失败");
  const retryReminder = (reminder: TodoReminder) => mutate(async () => {
    await api(`/api/todos/reminders/${encodeURIComponent(reminder.id)}/retry`, { method: "POST" });
  }, "提醒重试失败");

  return { todos, reminders, diagnostics, settings, loading, loaded, error, busy, refresh, save, setStatus, readReminder, retryReminder, dismissError: () => setError("") };
}
