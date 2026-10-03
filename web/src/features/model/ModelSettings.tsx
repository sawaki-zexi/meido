import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { ModelConfiguration as Configuration, Provider } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";

type Draft = { providerId: string; provider: string; baseUrl: string; model: string; apiKey: string };

const emptyDraft: Draft = { providerId: "openai", provider: "openai", baseUrl: "", model: "", apiKey: "" };

export function ModelSettings() {
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

  const refreshConfigurations = async () => {
    const result = await api<{ configurations: Configuration[]; activeId: string | null }>("/api/model/configurations");
    setConfigurations(result.configurations);
    setActiveId(result.activeId);
    return result;
  };

  useEffect(() => {
    void Promise.all([api<{ providers: Provider[] }>("/api/model/providers"), refreshConfigurations()]).then(([providerData, configurationData]) => {
      setProviders(providerData.providers);
      const initial = providerData.providers.find((item) => item.id === "openai");
      if (initial) setDraft({ ...emptyDraft, provider: initial.provider, baseUrl: initial.baseUrl });
    }).catch((cause) => setError(errorMessage(cause, "无法加载模型配置"))).finally(() => setLoading(false));
  }, []);

  const editConfiguration = (configuration: Configuration) => {
    setEditingId(configuration.id);
    setDraft({ providerId: configuration.providerId, provider: configuration.provider, baseUrl: configuration.baseUrl, model: configuration.model, apiKey: "" });
    setTestResult("");
    setError("");
    requestAnimationFrame(() => formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const startNewConfiguration = () => {
    const initial = providers.find((item) => item.id === "openai");
    setEditingId(null);
    setDraft(initial ? { ...emptyDraft, provider: initial.provider, baseUrl: initial.baseUrl } : emptyDraft);
    setTestResult("");
    setError("");
    requestAnimationFrame(() => formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const updateProvider = (providerId: string) => {
    setTestResult("");
    if (providerId === "custom") {
      setDraft((current) => ({ ...current, providerId, provider: "", baseUrl: "", model: "", apiKey: "" }));
      return;
    }
    const preset = providers.find((item) => item.id === providerId);
    if (preset) setDraft((current) => ({ ...current, providerId, provider: preset.provider, baseUrl: preset.baseUrl, model: "", apiKey: "" }));
  };

  const submit = async (action: "test" | "save") => {
    setError("");
    setTestResult("");
    const setBusy = action === "test" ? setTesting : setSaving;
    setBusy(true);
    try {
      if (action === "test") {
        const result = await api<{ ok: boolean; message: string; latencyMs?: number }>("/api/model/configuration/test", { method: "POST", body: JSON.stringify(draft) });
        setTestResult(result.ok && result.latencyMs !== undefined ? `连接成功 · ${result.latencyMs} ms` : `失败：${result.message}`);
      } else {
        const path = editingId ? `/api/model/configurations/${encodeURIComponent(editingId)}` : "/api/model/configurations";
        const result = await api<{ configuration: Configuration }>(path, { method: editingId ? "PUT" : "POST", body: JSON.stringify(draft) });
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
      <div><h2 id="model-settings-title">模型服务</h2><p className="muted">配置本地对话使用的默认模型。</p></div>
      <button type="button" onClick={startNewConfiguration} disabled={loading || busy}>新建配置</button>
    </header>
    {configurations.length > 0 && <ul className="configuration-list" aria-label="已保存的模型配置">{configurations.map((configuration) => {
      const isActive = configuration.id === activeId;
      const isEditing = configuration.id === editingId;
      return <li className={`configuration-item${isActive ? " active" : ""}${isEditing ? " editing" : ""}`} key={configuration.id}>
        <button type="button" className="configuration-main" onClick={() => editConfiguration(configuration)}>
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
    {loading ? <Placeholder loading>正在加载…</Placeholder> : <form ref={formRef} className="card-form" onSubmit={(event) => { event.preventDefault(); void submit("save"); }}>
      <div className="settings-subheading">
        <h3>{editingId ? `编辑配置：${editingConfiguration?.provider} · ${editingConfiguration?.model}` : "新建配置"}</h3>
        {active && <span className="muted">当前生效：{active.provider} · {active.model}</span>}
      </div>
      <div className="field"><label htmlFor="model-provider">服务商</label><select id="model-provider" value={draft.providerId} disabled={busy} onChange={(event) => updateProvider(event.target.value)}>{providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}<option value="custom">自定义</option></select></div>
      {draft.providerId === "custom" && <div className="field"><label htmlFor="model-provider-id">服务商标识</label><input id="model-provider-id" required value={draft.provider} disabled={busy} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></div>}
      <div className="field"><label htmlFor="model-base-url">API 地址</label><input id="model-base-url" required type="url" value={draft.baseUrl} disabled={busy} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></div>
      <div className="field"><label htmlFor="model-id">模型 ID</label><input id="model-id" required value={draft.model} placeholder={preset?.modelHint ?? "例如：provider/model-name"} disabled={busy} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></div>
      <div className="field"><label htmlFor="model-api-key">API Key</label><input id="model-api-key" type="password" autoComplete="new-password" value={draft.apiKey} placeholder={editingConfiguration?.apiKeyConfigured ? "已设置，留空保留现有密钥" : "可选，本地模型服务可能不需要"} disabled={busy} onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} /></div>
      {testResult && <Notice tone={testFailed ? "error" : "success"}>{testResult}</Notice>}
      {error && <Notice>{error}</Notice>}
      <div className="form-actions">
        <button type="button" disabled={busy || loading} onClick={() => void submit("test")}>{testing ? "测试中…" : "测试连接"}</button>
        <button type="submit" className="primary" disabled={busy || loading}>{saving ? "保存中…" : "保存"}</button>
      </div>
    </form>}
  </section>;
}
