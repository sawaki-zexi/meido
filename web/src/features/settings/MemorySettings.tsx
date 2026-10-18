import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { EmbeddingConfiguration } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";
import { ApiKeyInput } from "../../ui/ApiKeyInput";
import { SettingsTabs } from "./SettingsTabs";

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
  const [tab, setTab] = useState<"general" | "embedding">("general");
  const [settings, setSettings] = useState<MemorySettingsState | null>(null);
  const [draft, setDraft] = useState(defaultDraft);
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<"settings" | "test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [embeddingError, setEmbeddingError] = useState("");
  const [embeddingMessage, setEmbeddingMessage] = useState("");
  const busy = pendingAction !== null;

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
    setPendingAction("settings"); setError(""); setMessage(""); setEmbeddingError(""); setEmbeddingMessage("");
    try {
      const result = await api<MemorySettingsState>("/api/memory/settings", { method: "PUT", body: JSON.stringify({ enabled: next.enabled ?? settings.enabled, embeddingEnabled: next.embeddingEnabled ?? settings.embeddingEnabled }) });
      setSettings(result);
      setMessage("记忆设置已保存");
    } catch (cause) { setError(errorMessage(cause, "记忆设置保存失败")); }
    finally { setPendingAction(null); }
  };

  const submitEmbedding = async (action: "test" | "save") => {
    setPendingAction(action); setError(""); setMessage(""); setEmbeddingError(""); setEmbeddingMessage("");
    try {
      if (action === "test") {
        const result = await api<{ ok: boolean; message: string; latencyMs?: number }>("/api/embedding/configuration/test", { method: "POST", body: JSON.stringify(draft) });
        setEmbeddingMessage(result.ok ? `连接成功${result.latencyMs !== undefined ? ` · ${result.latencyMs} ms` : ""}` : `失败：${result.message}`);
      } else {
        const result = await api<MemorySettingsState["embedding"]>("/api/embedding/configuration", { method: "PUT", body: JSON.stringify(draft) });
        setSettings((current) => current ? { ...current, embedding: result } : current);
        setDraft((current) => ({ ...current, apiKey: "" }));
        setEmbeddingMessage("向量模型配置已保存并生效");
      }
    } catch (cause) { setEmbeddingError(errorMessage(cause, action === "test" ? "向量模型连接测试失败" : "向量模型配置保存失败")); }
    finally { setPendingAction(null); }
  };

  if (loading) return <Placeholder loading>正在加载记忆设置…</Placeholder>;
  if (!settings) return <Notice>{error || "记忆设置不可用"}</Notice>;
  const embedding = settings.embedding.configuration;
  const embeddingStatus = settings.embedding.status;
  return <section className="memory-settings page-narrow" aria-labelledby="memory-settings-title">
    <header className="settings-heading"><div><h2 id="memory-settings-title">记忆</h2><p className="muted">管理角色记忆与共享向量模型。</p></div></header>
    <SettingsTabs label="记忆分类" items={[{ id: "general", label: "基础" }, { id: "embedding", label: "向量模型" }]} current={tab} onSelect={setTab} disabled={busy} />
    {error && <Notice>{error}</Notice>}
    {message && <Notice tone={message.startsWith("失败") ? "error" : "success"}>{message}</Notice>}
    {tab === "general" && <section className="card-form" aria-labelledby="memory-switch-title">
      <div className="settings-subheading"><div><h3 id="memory-switch-title">记忆功能</h3><p className="muted">暂停后保留已有记忆，已开始的任务会完成。</p></div><label className="plugin-switch" title="启用或暂停记忆"><input type="checkbox" checked={settings.enabled} disabled={busy} onChange={(event) => void saveSettings({ enabled: event.target.checked })} aria-label="启用记忆功能" /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
      <p className="muted">当前状态：{settings.enabled ? "已启用" : "已暂停"}</p>
    </section>}
    {tab === "embedding" && <section className="card-form" aria-labelledby="embedding-settings-title">
      <div className="settings-subheading"><div><h3 id="embedding-settings-title">向量模型</h3><p className="muted">主人资料与长期记忆共用；暂停后仍可按关键词查找。</p></div><label className="plugin-switch" title="启用或暂停向量模型"><input type="checkbox" checked={settings.embeddingEnabled} disabled={busy} onChange={(event) => void saveSettings({ embeddingEnabled: event.target.checked })} aria-label="启用向量模型" /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
      <div className="field"><label htmlFor="embedding-provider">服务商</label><input id="embedding-provider" value={draft.provider} disabled={busy} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-base-url">向量 API 地址</label><input id="embedding-base-url" required type="url" value={draft.baseUrl} disabled={busy} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-model-id">向量模型 ID</label><input id="embedding-model-id" required value={draft.model} disabled={busy} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-dimensions">向量维度</label><input id="embedding-dimensions" required type="number" min={1} max={4096} value={draft.dimensions} disabled={busy} onChange={(event) => setDraft({ ...draft, dimensions: Number(event.target.value) })} /></div>
      <ApiKeyInput id="embedding-api-key" label="向量 API Key" value={draft.apiKey} configured={embedding.apiKeyConfigured} placeholder="请输入 DashScope API Key" disabled={busy} onChange={(apiKey) => setDraft({ ...draft, apiKey })} onReveal={async () => {
        const result = await api<{ apiKey: string }>("/api/embedding/configuration/reveal-key", { method: "POST" });
        return result.apiKey;
      }} />
      <div className="form-actions"><button type="button" disabled={busy} onClick={() => void submitEmbedding("test")}>{pendingAction === "test" ? "测试中…" : "测试连接"}</button><button type="button" className="primary" disabled={busy} onClick={() => void submitEmbedding("save")}>{pendingAction === "save" ? "保存中…" : "保存向量配置"}</button></div>
      {embeddingStatus.lastError && <Notice>{embeddingStatus.lastError}</Notice>}
      {pendingAction === "test" && <p className="muted" role="status">正在测试向量模型连接…</p>}
      {embeddingError && <Notice>{embeddingError}</Notice>}
      {embeddingMessage && <Notice tone={embeddingMessage.startsWith("失败") ? "error" : "success"}>{embeddingMessage}</Notice>}
      <p className="muted">当前状态：{settings.embeddingEnabled ? (embeddingStatus.ready ? "可用" : embeddingStatus.configured ? "不可用" : "未配置") : "已暂停"} · {embedding.model} · {embedding.dimensions} 维</p>
    </section>}
  </section>;
}
