import { useEffect, useState } from "react";

type Provider = { id: string; label: string; provider: string; baseUrl: string; modelHint: string };
type Configuration = { providerId: string; provider: string; baseUrl: string; model: string; apiKeyConfigured: boolean };
type Draft = { providerId: string; provider: string; baseUrl: string; model: string; apiKey: string };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail ?? `请求失败 (${response.status})`);
  return data as T;
}

export function ModelSettings({ onBack }: { onBack: () => void }) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [saved, setSaved] = useState<Configuration | null>(null);
  const [draft, setDraft] = useState<Draft>({ providerId: "openai", provider: "openai", baseUrl: "", model: "", apiKey: "" });
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState("");

  useEffect(() => {
    void Promise.all([
      api<{ providers: Provider[] }>("/api/model/providers"),
      api<{ configuration: Configuration | null }>("/api/model/configuration"),
    ]).then(([providerData, configData]) => {
      setProviders(providerData.providers);
      setSaved(configData.configuration);
      if (configData.configuration) {
        const { providerId, provider, baseUrl, model } = configData.configuration;
        setDraft({ providerId, provider, baseUrl, model, apiKey: "" });
      } else {
        const initial = providerData.providers.find((item) => item.id === "openai");
        if (initial) setDraft((current) => ({ ...current, provider: initial.provider, baseUrl: initial.baseUrl }));
      }
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "无法加载模型配置"))
      .finally(() => setLoading(false));
  }, []);

  const updateProvider = (providerId: string) => {
    setTestResult("");
    if (providerId === "custom") {
      setDraft((current) => ({ ...current, providerId, provider: "", baseUrl: "", apiKey: "" }));
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
        const result = await api<{ ok: boolean; message: string }>("/api/model/configuration/test", { method: "POST", body: JSON.stringify(draft) });
        setTestResult(`${result.ok ? "成功" : "失败"}：${result.message}`);
      } else {
        const result = await api<{ configuration: Configuration }>("/api/model/configuration", { method: "PUT", body: JSON.stringify(draft) });
        setSaved(result.configuration);
        setDraft((current) => ({ ...current, apiKey: "" }));
        setTestResult("配置已保存并生效");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : action === "test" ? "连接测试失败" : "配置保存失败");
    } finally { setBusy(false); }
  };

  const preset = providers.find((item) => item.id === draft.providerId);
  return <section className="model-settings">
    <div className="settings-heading"><div><h2>模型服务</h2><p>配置本地对话使用的默认模型。</p></div><span>{saved ? "已保存配置" : "尚未保存"}</span></div>
    {loading ? <p>正在加载…</p> : <form onSubmit={(event) => { event.preventDefault(); void submit("save"); }}>
      <label>服务商<select value={draft.providerId} disabled={saving || testing} onChange={(event) => updateProvider(event.target.value)}>
        {providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        <option value="custom">自定义</option>
      </select></label>
      {draft.providerId === "custom" && <label>服务商标识<input required value={draft.provider} disabled={saving || testing} onChange={(event) => setDraft({ ...draft, provider: event.target.value })} /></label>}
      <label>API 地址<input required type="url" value={draft.baseUrl} disabled={saving || testing} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></label>
      <label>模型 ID<input required value={draft.model} placeholder={preset?.modelHint ?? "例如：provider/model-name"} disabled={saving || testing} onChange={(event) => setDraft({ ...draft, model: event.target.value })} /></label>
      <label>API Key<input type="password" autoComplete="new-password" value={draft.apiKey} placeholder={saved?.apiKeyConfigured ? "已设置，留空保留现有密钥" : "可选，本地模型服务可能不需要"} disabled={saving || testing} onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} /></label>
      <div className="form-actions">
        <button type="button" disabled={saving || testing || loading} onClick={() => void submit("test")}>{testing ? "测试中…" : "测试连接"}</button>
        <button type="submit" disabled={saving || testing || loading}>{saving ? "保存中…" : "保存"}</button>
        <button type="button" disabled={saving || testing} onClick={onBack}>返回</button>
      </div>
      {saved && <p className="saved-model">当前生效：{saved.provider} · {saved.model}{saved.apiKeyConfigured ? " · API Key 已设置" : " · 无 API Key"}</p>}
      {testResult && <p className={testResult.startsWith("失败") ? "error" : "success"} role="status">{testResult}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </form>}
  </section>;
}
