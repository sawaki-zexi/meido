import type { TodoItem, TodoStatus } from "../../api/types";

export type TodoFilter = "active" | "completed" | "cancelled" | "all";
export const filters: { id: TodoFilter; label: string }[] = [
  { id: "active", label: "未完成" }, { id: "completed", label: "已完成" },
  { id: "cancelled", label: "已取消" }, { id: "all", label: "全部" },
];
export const statusLabels: Record<TodoStatus, string> = { inbox: "未安排", scheduled: "已安排", completed: "已完成", cancelled: "已取消" };
export const isActive = (todo: TodoItem) => todo.status === "inbox" || todo.status === "scheduled";
export const matchesFilter = (todo: TodoItem, filter: TodoFilter) => filter === "all" || (filter === "active" ? isActive(todo) : todo.status === filter);

export function zonedParts(iso: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return { date: `${values.year}-${values.month}-${values.day}`, time: `${values.hour}:${values.minute}` };
}

export function dayLabel(day: string, today: string) {
  if (day === today) return "今天";
  const tomorrow = new Date(`${today}T12:00:00Z`);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (day === tomorrow.toISOString().slice(0, 10)) return "明天";
  const [, month, date] = day.split("-");
  return `${day.slice(0, 4) === today.slice(0, 4) ? "" : `${day.slice(0, 4)}年`}${Number(month)}月${Number(date)}日`;
}

/** Use the earliest relevant day, matching the reminder service's day-summary boundary. */
function scheduledDay(todo: TodoItem, timezone: string) {
  return [todo.dueDate, todo.reminderDate, todo.dueAt && zonedParts(todo.dueAt, timezone).date,
    todo.reminderAt && zonedParts(todo.reminderAt, timezone).date].filter((day): day is string => Boolean(day)).sort()[0];
}

export function groupTodos(todos: TodoItem[], today: string, timezone: string) {
  const groups = new Map<string, TodoItem[]>();
  for (const todo of todos) {
    const date = scheduledDay(todo, timezone);
    const key = isActive(todo) ? (!date ? "undated" : date < today ? "earlier" : date) : todo.status;
    groups.set(key, [...(groups.get(key) ?? []), todo]);
  }
  const rank = (key: string) => key === "earlier" ? "0" : key === "undated" ? "2" : key === "completed" ? "3" : key === "cancelled" ? "4" : `1${key}`;
  return [...groups].sort(([a], [b]) => rank(a).localeCompare(rank(b))).map(([key, items]) => ({
    key, items, label: key === "earlier" ? "之前" : key === "undated" ? "未安排" : key === "completed" ? "已完成" : key === "cancelled" ? "已取消" : dayLabel(key, today),
  }));
}

export function dateTimeLabel(iso: string, timezone: string, today: string) {
  const { date, time } = zonedParts(iso, timezone);
  return `${dayLabel(date, today)} ${time}`;
}

export function todoTiming(todo: TodoItem, timezone: string, today: string) {
  const labels = [];
  if (todo.dueAt) labels.push(`${dateTimeLabel(todo.dueAt, timezone, today)} 截止`);
  else if (todo.dueDate) labels.push(`${dayLabel(todo.dueDate, today)}截止`);
  if (todo.reminderAt) labels.push(`${dateTimeLabel(todo.reminderAt, timezone, today)} 提醒`);
  else if (todo.reminderDate) labels.push(`${dayLabel(todo.reminderDate, today)}提醒`);
  return labels.join(" · ");
}
