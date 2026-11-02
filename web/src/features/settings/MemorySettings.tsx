import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { EmbeddingConfiguration } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";
import { ApiKeyInput } from "../../ui/ApiKeyInput";
import { SettingsTabs } from "./SettingsTabs";
import { SettingsSwitch } from "./SettingsSwitch";

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

export function MemorySettings({ active = true, scope = "memory", embeddingOnly = false, onSaved }: {
  active?: boolean; scope?: "memory" | "owner"; embeddingOnly?: boolean; onSaved?: () => void;
}) {
  const owner = scope === "owner";
  const embeddingApi = owner ? "/api/embedding/configuration" : "/api/memory/embedding/configuration";
  const [tab, setTab] = useState<"general" | "embedding">("general");
  const [settings, setSettings] = useState<MemorySettingsState | null>(null);
  const [draft, setDraft] = useState(defaultDraft);
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<"settings" | "test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [embeddingError, setEmbeddingError] = useState("");
  const [embeddingMessage, setEmbeddingMessage] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const previouslyActive = useRef(active);
  const busy = pendingAction !== null || refreshing;

  const readSettings = async () => {
    if (!owner) return api<MemorySettingsState>("/api/memory/settings");
    const embedding = await api<MemorySettingsState["embedding"]>(embeddingApi);
    return { enabled: true, embeddingEnabled: embedding.status.enabled, embedding };
  };
  const refresh = async () => {
    const result = await readSettings();
    setSettings(result);
    const configuration = result.embedding.configuration;
    setDraft({ provider: configuration.provider, baseUrl: configuration.baseUrl, model: configuration.model, dimensions: configuration.dimensions, apiKey: "" });
  };

  useEffect(() => {
    void refresh().catch((cause) => setError(errorMessage(cause, "无法加载记忆设置"))).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    let current = true;
    if (active && !previouslyActive.current) {
      setRefreshing(true);
      void readSettings().then(value => { if (current) setSettings(value); })
        .catch(cause => { if (current) setError(errorMessage(cause, '无法刷新记忆设置')); })
        .finally(() => { if (current) setRefreshing(false); });
    }
    previouslyActive.current = active;
    return () => { current = false; };
  }, [active]);

  const saveSettings = async (next: Partial<Pick<MemorySettingsState, "enabled" | "embeddingEnabled">>) => {
    if (!settings) return;
    setPendingAction("settings"); setError(""); setMessage(""); setEmbeddingError(""); setEmbeddingMessage("");
    try {
      const saved = await api<MemorySettingsState>("/api/memory/settings", { method: "PUT", body: JSON.stringify(owner ? { ownerEmbeddingEnabled: next.embeddingEnabled } : next) });
      const result = owner ? await readSettings() : saved;
      setSettings(result);
      setMessage("记忆设置已保存");
      onSaved?.();
    } catch (cause) { setError(errorMessage(cause, "记忆设置保存失败")); }
    finally { setPendingAction(null); }
  };

  const submitEmbedding = async (action: "test" | "save") => {
    setPendingAction(action); setError(""); setMessage(""); setEmbeddingError(""); setEmbeddingMessage("");
    try {
      if (action === "test") {
        const result = await api<{ ok: boolean; message: string; latencyMs?: number }>(`${embeddingApi}/test`, { method: "POST", body: JSON.stringify(draft) });
        setEmbeddingMessage(result.ok ? `连接成功${result.latencyMs !== undefined ? ` · ${result.latencyMs} ms` : ""}` : `失败：${result.message}`);
      } else {
        const result = await api<MemorySettingsState["embedding"]>(embeddingApi, { method: "PUT", body: JSON.stringify(draft) });
        setSettings((current) => current ? { ...current, embedding: result } : current);
        setDraft((current) => ({ ...current, apiKey: "" }));
        setEmbeddingMessage("向量模型配置已保存并生效");
        onSaved?.();
      }
    } catch (cause) { setEmbeddingError(errorMessage(cause, action === "test" ? "向量模型连接测试失败" : "向量模型配置保存失败")); }
    finally { setPendingAction(null); }
  };

  if (loading) return <Placeholder loading>正在加载记忆设置…</Placeholder>;
  if (!settings) return <Notice>{error || "记忆设置不可用"}</Notice>;
  const embedding = settings.embedding.configuration;
  const embeddingStatus = settings.embedding.status;
  return <section className="memory-settings page-narrow" aria-labelledby="memory-settings-title">
    {!embeddingOnly && <><header className="settings-heading"><h2 id="memory-settings-title">记忆</h2></header>
    <SettingsTabs label="记忆分类" items={[{ id: "general", label: "基础" }, { id: "embedding", label: "向量模型" }]} current={tab} onSelect={setTab} disabled={busy} /></>}
    {error && <Notice>{error}</Notice>}
    {message && <Notice tone={message.startsWith("失败") ? "error" : "success"}>{message}</Notice>}
    {!embeddingOnly && tab === "general" && <section className="card-form" aria-labelledby="memory-switch-title">
      <div className="settings-preference"><div><h3 id="memory-switch-title">启用语义记忆引擎</h3><p className="muted">控制语义检索与记忆工具。关闭后宿主记忆文档仍会整理，并可能调用聊天模型；周期优化由高级设置独立控制。</p></div><SettingsSwitch label="启用语义记忆引擎" checked={settings.enabled} disabled={busy} onChange={enabled => void saveSettings({ enabled })} /></div>
    </section>}
    {(embeddingOnly || tab === "embedding") && <section className="card-form" aria-labelledby="embedding-settings-title">
      <div className="settings-preference"><div><h3 id="embedding-settings-title">{owner ? "主人资料向量模型" : "向量模型"}</h3><p className="muted">{owner ? "用于主人资料的语义检索，与角色记忆引擎分别配置。" : "语义记忆引擎的独立配置，不改变主人资料的向量模型。模型或维度与已有空间不符时引擎不可用，保留旧数据。"}</p></div><SettingsSwitch label={owner ? "启用主人资料向量模型" : "启用记忆引擎向量模型"} checked={settings.embeddingEnabled} disabled={busy} onChange={embeddingEnabled => void saveSettings({ embeddingEnabled })} /></div>
      <div className="field"><label htmlFor="embedding-provider">服务商</label><input id="embedding-provider" value={draft.provider} disabled={busy} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-base-url">向量 API 地址</label><input id="embedding-base-url" required type="url" value={draft.baseUrl} disabled={busy} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-model-id">向量模型 ID</label><input id="embedding-model-id" required value={draft.model} disabled={busy} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></div>
      <div className="field"><label htmlFor="embedding-dimensions">向量维度</label><input id="embedding-dimensions" required type="number" min={1} max={4096} value={draft.dimensions} disabled={busy} onChange={(event) => setDraft({ ...draft, dimensions: Number(event.target.value) })} /></div>
      <ApiKeyInput id="embedding-api-key" label="向量 API Key" value={draft.apiKey} configured={embedding.apiKeyConfigured} placeholder="请输入 DashScope API Key" disabled={busy || !active} onChange={(apiKey) => setDraft({ ...draft, apiKey })} onReveal={async () => {
        const result = await api<{ apiKey: string }>(`${embeddingApi}/reveal-key`, { method: "POST" });
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
