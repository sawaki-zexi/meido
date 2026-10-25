import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { ModelConfiguration as Configuration, Provider } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";
import { ApiKeyInput } from "../../ui/ApiKeyInput";
import { Icon } from "../../ui/Icon";

type Draft = { providerId: string; provider: string; api: string; apiOptions: Record<string, string>; baseUrl: string; model: string; apiKey: string; contextWindowTokens: string; modelAutoCompactTokenLimit: string };

const modelApis = [
  { value: "openai-completions", label: "OpenAI Chat Completions" },
  { value: "openai-responses", label: "OpenAI Responses" },
  { value: "azure-openai-responses", label: "Azure OpenAI Responses" },
  { value: "openai-codex-responses", label: "OpenAI Codex Responses" },
  { value: "anthropic-messages", label: "Anthropic Messages" },
  { value: "google-generative", label: "Google Generative AI" },
  { value: "google-vertex", label: "Google Vertex AI" },
  { value: "mistral-conversations", label: "Mistral Conversations" },
  { value: "bedrock-converse", label: "Amazon Bedrock Converse" },
  { value: "pi-messages", label: "Pi Messages" },
];

const emptyDraft: Draft = { providerId: "openai", provider: "openai", api: "openai-completions", apiOptions: {}, baseUrl: "", model: "", apiKey: "", contextWindowTokens: '', modelAutoCompactTokenLimit: '' };

export function ModelSettings({ active: pageActive = true }: { active?: boolean }) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [configurations, setConfigurations] = useState<Configuration[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState("");
  const formRef = useRef<HTMLFormElement>(null);
  const previouslyActive = useRef(pageActive);

  const refreshConfigurations = async () => {
    const result = await api<{ configurations: Configuration[]; activeId: string | null }>("/api/model/configurations");
    setConfigurations(result.configurations);
    setActiveId(result.activeId);
    return result;
  };

  useEffect(() => {
    void Promise.all([api<{ providers: Provider[] }>("/api/model/providers"), refreshConfigurations()]).then(([providerData, modelData]) => {
      setProviders(providerData.providers);
      setEditorOpen(modelData.configurations.length === 0);
      const initial = providerData.providers.find((item) => item.id === "openai");
      if (initial) setDraft({ ...emptyDraft, provider: initial.provider, api: initial.api ?? "openai-completions", baseUrl: initial.baseUrl });
    }).catch((cause) => setError(errorMessage(cause, "无法加载模型配置"))).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (pageActive && !previouslyActive.current) {
      void refreshConfigurations().catch(cause => setError(errorMessage(cause, '无法刷新模型列表')));
    }
    previouslyActive.current = pageActive;
  }, [pageActive]);

  const editConfiguration = (configuration: Configuration) => {
    setEditorOpen(true);
    setEditingId(configuration.id);
    setDraft({ providerId: configuration.providerId, provider: configuration.provider, api: configuration.api ?? "openai-completions", apiOptions: configuration.apiOptions ?? {}, baseUrl: configuration.baseUrl, model: configuration.model, apiKey: "",
      contextWindowTokens: String(configuration.contextWindowTokens ?? ''), modelAutoCompactTokenLimit: String(configuration.modelAutoCompactTokenLimit ?? '') });
    setTestResult("");
    setError("");
    requestAnimationFrame(() => formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const startNewConfiguration = () => {
    setEditorOpen(true);
    const initial = providers.find((item) => item.id === "openai");
    setEditingId(null);
    setDraft(initial ? { ...emptyDraft, provider: initial.provider, api: initial.api ?? "openai-completions", baseUrl: initial.baseUrl } : emptyDraft);
    setTestResult("");
    setError("");
    requestAnimationFrame(() => formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const updateProvider = (providerId: string) => {
    setTestResult("");
    if (providerId === "custom") {
      setDraft((current) => ({ ...current, providerId, provider: "", api: "openai-completions", apiOptions: {}, baseUrl: "", model: "", apiKey: "" }));
      return;
    }
    const preset = providers.find((item) => item.id === providerId);
    if (preset) setDraft((current) => ({ ...current, providerId, provider: preset.provider, api: preset.api ?? "openai-completions", apiOptions: {}, baseUrl: preset.baseUrl, model: "", apiKey: "" }));
  };

  const submit = async (action: "test" | "save") => {
    setError("");
    setTestResult("");
    const setBusy = action === "test" ? setTesting : setSaving;
    setBusy(true);
    const payload = { ...draft, contextWindowTokens: draft.contextWindowTokens === '' ? null : Number(draft.contextWindowTokens),
      modelAutoCompactTokenLimit: draft.modelAutoCompactTokenLimit === '' ? null : Number(draft.modelAutoCompactTokenLimit) };
    try {
      if (action === "test") {
        const result = await api<{ ok: boolean; message: string; latencyMs?: number }>("/api/model/configuration/test", { method: "POST", body: JSON.stringify(payload) });
        setTestResult(result.ok && result.latencyMs !== undefined ? `连接成功 · ${result.latencyMs} ms` : `失败：${result.message}`);
      } else {
        const path = editingId ? `/api/model/configurations/${encodeURIComponent(editingId)}` : "/api/model/configurations";
        const result = await api<{ configuration: Configuration }>(path, { method: editingId ? "PUT" : "POST", body: JSON.stringify(payload) });
        setEditingId(result.configuration.id);
        setDraft((current) => ({ ...current, apiKey: "" }));
        await refreshConfigurations();
        setTestResult("配置已保存并生效");
      }
    } catch (cause) { setError(errorMessage(cause, action === "test" ? "连接测试失败" : "配置保存失败")); }
    finally { setBusy(false); }
  };

  const activate = async (configuration: Configuration) => {
    setError("");
    try {
      await api(`/api/model/configurations/${encodeURIComponent(configuration.id)}/activate`, { method: "POST" });
      await refreshConfigurations();
      editConfiguration(configuration);
      setTestResult("配置已切换并生效");
    } catch (cause) { setError(errorMessage(cause, "配置切换失败")); }
  };

  const remove = async (configuration: Configuration) => {
    if (!window.confirm(`确定删除“${configuration.provider} · ${configuration.model}”吗？\n\n此配置将永久删除。`)) return;
    setError("");
    try {
      const result = await api<{ activeId: string | null }>(`/api/model/configurations/${encodeURIComponent(configuration.id)}`, { method: "DELETE" });
      const refreshed = await refreshConfigurations();
      const next = refreshed.configurations.find((item) => item.id === result.activeId);
      if (editingId === configuration.id) {
        if (next) editConfiguration(next); else startNewConfiguration();
      }
    } catch (cause) { setError(errorMessage(cause, "配置删除失败")); }
  };

  const preset = providers.find((item) => item.id === draft.providerId);
  const active = configurations.find((item) => item.id === activeId);
  const editingConfiguration = configurations.find((item) => item.id === editingId);
  const busy = saving || testing;
  const testFailed = testResult.startsWith("失败");

  return <section className="model-settings page-narrow" aria-labelledby="model-settings-title">
    <header className="settings-heading">
      <div><h2 id="model-settings-title">模型</h2></div>
      {!editorOpen && <button type="button" onClick={startNewConfiguration} disabled={loading || busy}><Icon name="plus" size={16} />新建配置</button>}
    </header>
    {!editorOpen && configurations.length > 0 && <ul className="configuration-list" aria-label="已保存的模型配置">{configurations.map((configuration) => {
      const isActive = configuration.id === activeId;
      const isEditing = editorOpen && configuration.id === editingId;
      return <li className={`configuration-item${isActive ? " active" : ""}${isEditing ? " editing" : ""}`} key={configuration.id}>
        <button type="button" className="configuration-main" disabled={busy} onClick={() => editConfiguration(configuration)}>
          <strong>{configuration.provider} · {configuration.model}</strong>
          <span>{configuration.baseUrl}</span>
          <small>{isActive ? "当前生效" : "未生效"}{configuration.apiKeyConfigured ? " · API Key 已设置" : " · 无 API Key"}{isEditing ? " · 正在编辑" : ""}</small>
        </button>
        <div className="configuration-actions">
          <button type="button" onClick={() => void activate(configuration)} disabled={isActive || busy}>{isActive ? "当前使用" : "设为生效"}</button>
          <button type="button" onClick={() => editConfiguration(configuration)} disabled={busy} aria-pressed={isEditing}>编辑</button>
          <button type="button" className="danger" onClick={() => void remove(configuration)} disabled={busy}>删除</button>
        </div>
      </li>;
    })}</ul>}
    {!editorOpen && error && <Notice>{error}</Notice>}
    {!loading && !editorOpen && configurations.length === 0 && <p className="muted">尚未添加模型连接。</p>}
    {loading ? <Placeholder loading>正在加载…</Placeholder> : editorOpen && <form ref={formRef} className="card-form" onSubmit={(event) => { event.preventDefault(); void submit("save"); }}>
      <div className="settings-subheading">
        <button type="button" className="ghost settings-back" disabled={busy} onClick={() => setEditorOpen(false)}><Icon name="back" size={16} />返回模型列表</button>
        <h3>{editingId ? `${editingConfiguration?.provider} · ${editingConfiguration?.model}` : "新建配置"}</h3>
        {active && <span className="muted">当前生效：{active.provider} · {active.model}</span>}
      </div>
      <div className="field"><label htmlFor="model-provider">服务商</label><select id="model-provider" value={draft.providerId} disabled={busy} onChange={(event) => updateProvider(event.target.value)}>{providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}<option value="custom">自定义</option></select></div>
      {draft.providerId === "custom" && <div className="field"><label htmlFor="model-provider-id">服务商标识</label><input id="model-provider-id" required value={draft.provider} disabled={busy} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></div>}
      <div className="field"><label htmlFor="model-api">API 协议</label><select id="model-api" value={draft.api} disabled={busy || draft.providerId !== "custom"} onChange={(event) => setDraft({ ...draft, api: event.target.value, apiOptions: event.target.value === "google-vertex" ? { location: "us-central1" } : {} })}>{modelApis.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></div>
      {draft.api === "azure-openai-responses" && <div className="field"><label htmlFor="azure-deployment">Azure 部署名称</label><input id="azure-deployment" value={draft.apiOptions.deploymentName ?? ""} disabled={busy} onChange={(event) => setDraft({ ...draft, apiOptions: { ...draft.apiOptions, deploymentName: event.target.value } })} /></div>}
      {draft.api === "google-vertex" && <>
        <div className="field"><label htmlFor="google-project">Google Cloud 项目 ID</label><input id="google-project" required value={draft.apiOptions.project ?? ""} disabled={busy} onChange={(event) => setDraft({ ...draft, apiOptions: { ...draft.apiOptions, project: event.target.value } })} /></div>
        <div className="field"><label htmlFor="google-location">Google Cloud 区域</label><input id="google-location" required value={draft.apiOptions.location ?? "us-central1"} disabled={busy} onChange={(event) => setDraft({ ...draft, apiOptions: { ...draft.apiOptions, location: event.target.value } })} /></div>
      </>}
      {draft.api === "openai-codex-responses" && <div className="field"><label htmlFor="codex-account-id">ChatGPT Account ID（可选）</label><input id="codex-account-id" value={draft.apiOptions.accountId ?? ""} disabled={busy} onChange={(event) => setDraft({ ...draft, apiOptions: { ...draft.apiOptions, accountId: event.target.value } })} /></div>}
      <div className="field"><label htmlFor="model-base-url">API 地址</label><input id="model-base-url" required type="url" value={draft.baseUrl} disabled={busy} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></div>
      <div className="field"><label htmlFor="model-id">模型 ID</label><input id="model-id" required value={draft.model} placeholder={preset?.modelHint ?? "例如：provider/model-name"} disabled={busy} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></div>
      <div className="field"><label htmlFor="model-context-window">上下文窗口（token）</label><input id="model-context-window" type="number" min="1" max="10000000" step="1" value={draft.contextWindowTokens} disabled={busy}
        onChange={event => { setDraft({ ...draft, contextWindowTokens: event.target.value }); setTestResult(''); }} />
        <small>留空不启用自动压缩。</small></div>
      <div className="field"><label htmlFor="model-auto-compact">自动压缩阈值（token，可选）</label><input id="model-auto-compact" type="number" min="1" max={draft.contextWindowTokens ? Number(draft.contextWindowTokens) - 1 : undefined} step="1" value={draft.modelAutoCompactTokenLimit} disabled={busy}
        onChange={event => { setDraft({ ...draft, modelAutoCompactTokenLimit: event.target.value }); setTestResult(''); }} />
        <small>小于上下文容量；留空使用全局比例。</small></div>
      <ApiKeyInput key={`${editingId ?? "new"}:${draft.providerId}:${draft.api}`} id="model-api-key" label={draft.api === "openai-codex-responses" ? "ChatGPT OAuth Token" : "API Key"} value={draft.apiKey} configured={Boolean(editingConfiguration?.apiKeyConfigured)} placeholder={draft.api === "openai-codex-responses" ? "粘贴 ChatGPT OAuth Token" : "可选，本地模型服务可能不需要"} disabled={busy || !pageActive} onChange={(apiKey) => setDraft({ ...draft, apiKey })} onReveal={async () => {
        if (!editingId) return "";
        const result = await api<{ apiKey: string }>(`/api/model/configurations/${encodeURIComponent(editingId)}/reveal-key`, { method: "POST" });
        return result.apiKey;
      }} />
      {testResult && <Notice tone={testFailed ? "error" : "success"}>{testResult}</Notice>}
      {error && <Notice>{error}</Notice>}
      <div className="form-actions">
        <button type="button" className="ghost" disabled={busy} onClick={() => { setEditorOpen(false); setDraft(emptyDraft); setEditingId(null); setError(""); setTestResult(""); }}>取消</button>
        <button type="button" disabled={busy || loading} onClick={() => void submit("test")}>{testing ? "测试中…" : "测试连接"}</button>
        <button type="submit" className="primary" disabled={busy || loading}>{saving ? "保存中…" : "保存"}</button>
      </div>
    </form>}
  </section>;
}
