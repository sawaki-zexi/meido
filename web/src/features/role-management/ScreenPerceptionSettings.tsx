import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { api, errorMessage } from "../../api/client";
import type { ModelConfiguration } from "../../api/types";
import { SettingsDialog } from "../settings/SettingsDialog";
import type { RoleDraft } from "./draft";

export function ScreenPerceptionSettings({ draft, onChange, disabled }: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  const [configurations, setConfigurations] = useState<ModelConfiguration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const hintId = useId();

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError("");
    void api<{ configurations: ModelConfiguration[] }>("/api/model/configurations")
      .then((data) => { if (current) setConfigurations(data.configurations); })
      .catch((cause) => { if (current) setError(errorMessage(cause, "无法加载视觉模型连接")); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [revision]);

  const selectedId = draft.screenVisionModelConfigurationId;
  const selected = configurations.find((item) => item.id === selectedId);
  const missing = !loading && !error && Boolean(selectedId) && !selected;
  return <section aria-label="屏幕感知配置">
    <label className="field checkbox-field">
      <input type="checkbox" aria-label="允许按需观察主屏" aria-describedby={hintId} checked={draft.screenPerceptionEnabled} disabled={disabled} onChange={(event) => onChange({ ...draft, screenPerceptionEnabled: event.target.checked })} />
      <span>允许按需观察主屏</span>
      <small className="field-hint" id={hintId}>画面发送到所选视觉连接，本地不保存原图；分析摘要会用于当前对话</small>
    </label>
    <label className="field">
      <span>视觉模型连接</span>
      <select
        aria-label="视觉模型连接"
        value={selectedId}
        required={draft.screenPerceptionEnabled}
        disabled={disabled || loading || Boolean(error)}
        ref={(node) => node?.setCustomValidity(draft.screenPerceptionEnabled && missing ? "连接已不存在，请重新选择视觉模型连接" : "")}
        onChange={(event) => onChange({ ...draft, screenVisionModelConfigurationId: event.target.value })}
      >
        <option value="">请选择已保存的模型连接</option>
        {selectedId && !selected && <option value={selectedId} disabled>{missing ? "连接已不存在" : "已选连接"}（{selectedId}）</option>}
        {configurations.map((configuration) => <option key={configuration.id} value={configuration.id}>
          {configuration.provider} · {configuration.model} · {configuration.api ?? "openai-completions"} · {configuration.id}
        </option>)}
      </select>
      {selected && <small style={{ color: "var(--ink-soft)", overflowWrap: "anywhere" }}>所选连接：{selected.provider} · {selected.model} · {selected.api ?? "openai-completions"}（ID：{selected.id}）</small>}
      <small className="field-hint">请明确选择视觉连接；所选模型的视觉能力会在调用时验证。</small>
    </label>
    {loading && <p role="status">正在加载视觉模型连接…</p>}
    {missing && <p role="status">连接已不存在，请重新选择视觉模型连接。</p>}
    {error && <p role="status">{error}</p>}
    {!loading && !error && configurations.length === 0 && <p>暂无已保存的模型连接，请先在模型设置中添加。</p>}
    <button type="button" className="ghost" disabled={disabled || loading} onClick={() => setRevision((value) => value + 1)}>刷新视觉连接</button>
    <button type="button" className="ghost" disabled={disabled} onClick={() => setSettingsOpen(true)}>打开模型设置</button>
    {settingsOpen && createPortal(<div onSubmit={(event) => event.stopPropagation()}>
      <SettingsDialog initialSection="model" onClose={() => { setSettingsOpen(false); setRevision((value) => value + 1); }} />
    </div>, document.body)}
  </section>;
}
