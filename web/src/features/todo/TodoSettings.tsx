import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { TodoReminderDay, TodoReminderRole, TodoSettings as TodoSettingsData } from "../../api/types";
import { Notice } from "../../ui/Status";

export function TodoSettings() {
  const [settings, setSettings] = useState<TodoSettingsData | null>(null);
  const [timezone, setTimezone] = useState("");
  const [reminderRoles, setReminderRoles] = useState<TodoReminderRole[]>([]);
  const [today, setToday] = useState<TodoReminderDay | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const loadReminders = async () => {
    const [roles, day] = await Promise.all([
      api<{ roles: TodoReminderRole[] }>("/api/todos/reminder-roles"),
      api<TodoReminderDay>("/api/todos/reminders/today"),
    ]);
    setReminderRoles(roles.roles);
    setToday(day);
  };

  useEffect(() => {
    void api<TodoSettingsData>("/api/todos/settings").then(async (loaded) => {
      const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const value = loaded.timezoneConfigured === false && detected
        ? await api<TodoSettingsData>("/api/todos/settings", { method: "PUT", body: JSON.stringify({ timezone: detected }) })
        : loaded;
      setSettings(value);
      setTimezone(value.timezone);
    }).catch((cause) => setError(errorMessage(cause, "无法读取待办设置")));
  }, []);

  useEffect(() => {
    if (!settings?.enabled || !settings.remindersEnabled) return;
    void loadReminders().catch((cause) => setError(errorMessage(cause, "无法读取提醒配置")));
  }, [settings?.enabled, settings?.remindersEnabled]);

  const update = async (patch: Partial<TodoSettingsData>) => {
    setSaving(true);
    setError("");
    try {
      const value = await api<TodoSettingsData>("/api/todos/settings", { method: "PUT", body: JSON.stringify(patch) });
      setSettings(value);
      setTimezone(value.timezone);
    } catch (cause) {
      setError(errorMessage(cause, "待办设置保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const toggleRole = async (role: TodoReminderRole) => {
    setSaving(true);
    setError("");
    try {
      await api<{ roleId: string; enabled: boolean; effectiveDate: string }>(`/api/todos/reminder-roles/${encodeURIComponent(role.id)}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !role.enabled }),
      });
      await loadReminders();
    } catch (cause) {
      setError(errorMessage(cause, "提醒角色保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const setPausedToday = async (paused: boolean) => {
    setSaving(true);
    setError("");
    try {
      setToday(await api<TodoReminderDay>(`/api/todos/reminders/today?paused=${paused}`, { method: "PUT" }));
    } catch (cause) {
      setError(errorMessage(cause, "今日提醒状态保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const hasReminderRoleToday = reminderRoles.some((role) => role.activeToday);
  const hasReminderRoleScheduled = reminderRoles.some((role) => role.enabled);

  return <section className="todo-settings">
    <h2>待办与提醒</h2>
    {settings && <>
      <label className="field checkbox-field">
        <input type="checkbox" checked={settings.enabled} disabled={saving} onChange={(event) => void update({ enabled: event.target.checked })} />
        <span>启用待办插件</span>
      </label>
      <label className="field checkbox-field">
        <input type="checkbox" checked={settings.remindersEnabled} disabled={saving || !settings.enabled} onChange={(event) => void update({ remindersEnabled: event.target.checked })} />
        <span>启用提醒</span>
      </label>
      <div className="field">
        <label htmlFor="todo-timezone">主人时区</label>
        <input id="todo-timezone" value={timezone} disabled={saving} placeholder="Asia/Shanghai" onChange={(event) => setTimezone(event.target.value)} onBlur={() => { if (timezone !== settings.timezone) void update({ timezone }); }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); } }} />
      </div>
      {settings.enabled && settings.remindersEnabled && <>
        <fieldset className="todo-reminder-roles">
          <legend>提醒角色</legend>
          <p className="todo-muted">每天会从已启用的角色中随机选一位负责当天全部提醒；关闭立即生效，启用从次日生效。</p>
          {reminderRoles.length ? <ul>{reminderRoles.map((role) => <li key={role.id}>
            <label className="field checkbox-field">
              <input type="checkbox" checked={role.enabled} disabled={saving} onChange={() => void toggleRole(role)} />
              <span>{role.name}</span>
            </label>
            {role.effectiveDate > (today?.date ?? "") && (role.activeToday === undefined || role.activeToday !== role.enabled) && <small className="todo-muted">{role.effectiveDate} 起生效</small>}
          </li>)}</ul> : <p className="todo-muted">还没有可用角色。</p>}
          {reminderRoles.length > 0 && !hasReminderRoleToday && <p className="todo-muted">
            {hasReminderRoleScheduled ? "提醒角色从生效日期起参与；今天不会发送提醒。" : "未启用提醒角色，系统不会发送提醒。"}
          </p>}
        </fieldset>
        <label className="field checkbox-field">
          <input type="checkbox" checked={Boolean(today?.paused)} disabled={saving} onChange={(event) => void setPausedToday(event.target.checked)} />
          <span>暂停今天的提醒</span>
        </label>
        {today?.paused && <p className="todo-muted">今天的提醒已暂停，明天自动恢复；暂停期间跳过的提醒不会补发。</p>}
      </>}
    </>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
  </section>;
}
