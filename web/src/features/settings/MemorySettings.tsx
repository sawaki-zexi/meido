import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { EmbeddingConfiguration } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";

type EmbeddingDraft = { provider: string; baseUrl: string; model: string; dimensions: number; apiKey: string };
type MemorySettingsState = {
  enabled: boolean;
  embeddingEnabled: boolean;
  embedding: { configuration: EmbeddingConfiguration; status: { configured: boolean; ready: boolean; enabled: boolean; lastError?: string | null } };
};

const defaultDraft: EmbeddingDraft = {
  provider: "dashscope",
  baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  model: "text-embedding-v3",
  dimensions: 1024,
  apiKey: "",
};

export function MemorySettings() {
  const [settings, setSettings] = useState<MemorySettingsState | null>(null);
  const [draft, setDraft] = useState(defaultDraft);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    const result = await api<MemorySettingsState>("/api/memory/settings");
    setSettings(result);
    const configuration = result.embedding.configuration;
    setDraft({ provider: configuration.provider, baseUrl: configuration.baseUrl, model: configuration.model, dimensions: configuration.dimensions, apiKey: "" });
  };

  useEffect(() => {
    void refresh().catch((cause) => setError(errorMessage(cause, "无法加载记忆设置"))).finally(() => setLoading(false));
  }, []);

  const saveSettings = async (next: Partial<Pick<MemorySettingsState, "enabled" | "embeddingEnabled">>) => {
    if (!settings) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api<MemorySettingsState>("/api/memory/settings", { method: "PUT", body: JSON.stringify({ enabled: next.enabled ?? settings.enabled, embeddingEnabled: next.embeddingEnabled ?? settings.embeddingEnabled }) });
      setSettings(result);
      setMessage("记忆设置已保存");
    } catch (cause) { setError(errorMessage(cause, "记忆设置保存失败")); }
    finally { setBusy(false); }
  };

  const submitEmbedding = async (action: "test" | "save") => {
    setBusy(true); setError(""); setMessage("");
    try {
      if (action === "test") {
        const result = await api<{ ok: boolean; message: string; latencyMs?: number }>("/api/embedding/configuration/test", { method: "POST", body: JSON.stringify(draft) });
        setMessage(result.ok && result.latencyMs !== undefined ? `连接成功 · ${result.latencyMs} ms` : `失败：${result.message}`);
      } else {
        const result = await api<MemorySettingsState["embedding"]>("/api/embedding/configuration", { method: "PUT", body: JSON.stringify(draft) });
        setSettings((current) => current ? { ...current, embedding: result } : current);
        setDraft((current) => ({ ...current, apiKey: "" }));
        setMessage("向量模型配置已保存并生效");
      }
    } catch (cause) { setError(errorMessage(cause, action === "test" ? "向量模型连接测试失败" : "向量模型配置保存失败")); }
    finally { setBusy(false); }
  };

  if (loading) return <Placeholder loading>正在加载记忆设置…</Placeholder>;
  if (!settings) return <Notice>{error || "记忆设置不可用"}</Notice>;
  const embedding = settings.embedding.configuration;
  const embeddingStatus = settings.embedding.status;
  return <section className="memory-settings page-narrow" aria-labelledby="memory-settings-title">
    <header className="settings-heading"><div><h2 id="memory-settings-title">记忆</h2><p className="muted">管理角色记忆、主人资料和共享向量模型。</p></div></header>
    {error && <Notice>{error}</Notice>}
    {message && <Notice tone={message.startsWith("失败") ? "error" : "success"}>{message}</Notice>}
    <section className="card-form" aria-labelledby="memory-switch-title">
      <div className="settings-subheading"><div><h3 id="memory-switch-title">记忆功能</h3><p className="muted">暂停角色记忆召回、自动提取和后台整理，保留已有记忆。已开始的任务会完成，主人资料仍由其自身开关控制。</p></div><label className="plugin-switch" title="启用或暂停记忆"><input type="checkbox" checked={settings.enabled} disabled={busy} onChange={(event) => void saveSettings({ enabled: event.target.checked })} aria-label="启用记忆功能" /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
      <p className="muted">当前状态：{settings.enabled ? "运行中" : "已暂停"}</p>
    </section>
    <section className="card-form" aria-labelledby="embedding-settings-title">
      <div className="settings-subheading"><div><h3 id="embedding-settings-title">向量模型</h3><p className="muted">主人资料与长期记忆共用；暂停后仍可按关键词查找。</p></div><label className="plugin-switch" title="启用或暂停向量模型"><input type="checkbox" checked={settings.embeddingEnabled} disabled={busy} onChange={(event) => void saveSettings({ embeddingEnabled: event.target.checked })} aria-label="启用向量模型" /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
      <div className="field"><label htmlFor="embedding-provider">服务商</label><input id="embedding-provider" value={draft.provider} disabled={busy} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-base-url">向量 API 地址</label><input id="embedding-base-url" required type="url" value={draft.baseUrl} disabled={busy} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-model-id">向量模型 ID</label><input id="embedding-model-id" required value={draft.model} disabled={busy} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-dimensions">向量维度</label><input id="embedding-dimensions" required type="number" min={1} max={4096} value={draft.dimensions} disabled={busy} onChange={(event) => setDraft({ ...draft, dimensions: Number(event.target.value) })} /></div>
      <div className="field"><label htmlFor="embedding-api-key">向量 API Key</label><input id="embedding-api-key" type="password" autoComplete="new-password" value={draft.apiKey} placeholder={embedding.apiKeyConfigured ? "已设置，留空保留现有密钥" : "请输入 DashScope API Key"} disabled={busy} onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} /></div>
      {embeddingStatus.lastError && <Notice>{embeddingStatus.lastError}</Notice>}
      <div className="form-actions"><button type="button" disabled={busy} onClick={() => void submitEmbedding("test")}>测试连接</button><button type="button" className="primary" disabled={busy} onClick={() => void submitEmbedding("save")}>保存向量配置</button></div>
      <p className="muted">当前状态：{settings.embeddingEnabled ? (embeddingStatus.ready ? "可用" : embeddingStatus.configured ? "不可用" : "未配置") : "已暂停"} · {embedding.model} · {embedding.dimensions} 维</p>
    </section>
  </section>;
}
