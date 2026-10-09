import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { TodoSettings as TodoSettingsData } from "../../api/types";
import { Notice } from "../../ui/Status";

export function TodoSettings() {
  const [settings, setSettings] = useState<TodoSettingsData | null>(null);
  const [timezone, setTimezone] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

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
    </>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
  </section>;
}
