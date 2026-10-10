import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Icon } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";

type SyncReport = {
  trigger: string;
  complete: boolean;
  created: number;
  updated: number;
  deleted: number;
  skipped: number;
  failed: number;
  unchanged: number;
  errors: string[];
  finished_at?: string;
  finishedAt?: string;
};

type PluginStatus = {
  enabled: boolean;
  connected: boolean;
  oauthConfigured: boolean;
  embeddingReady: boolean;
  lastError: string | null;
  lastSync: SyncReport | null;
  redirectUri?: string;
};
type OwnerRole = { id: string; name: string };
type OwnerUnderstanding = { roleId: string; body: string; updatedAt: string; version: number };
type OwnerDocument = { documentId: string; objectType: string; title: string; url: string; status: string; error: string };

export function OwnerKnowledgeSettings() {
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [roles, setRoles] = useState<OwnerRole[]>([]);
  const [documents, setDocuments] = useState<OwnerDocument[]>([]);
  const [selectedRoleId, setSelectedRoleId] = useState("");
  const [understanding, setUnderstanding] = useState<OwnerUnderstanding | null>(null);
  const [loadingUnderstanding, setLoadingUnderstanding] = useState(false);
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    const [result, roleData, documentData] = await Promise.all([
      api<PluginStatus & { appId?: string }>("/api/owner-knowledge"),
      api<{ roles: OwnerRole[] }>("/api/roles"),
      api<{ documents: OwnerDocument[] }>("/api/owner-knowledge/documents"),
    ]);
    setStatus(result);
    setRoles(roleData.roles);
    setDocuments(documentData.documents);
    setSelectedRoleId((current) => current || roleData.roles[0]?.id || "");
    if (result.appId) setAppId(result.appId);
  };

  useEffect(() => {
    const currentUrl = new URL(window.location.href);
    if (currentUrl.searchParams.get("ownerKnowledge") === "connected") {
      setMessage("飞书授权成功，可以启用主人资料插件");
      currentUrl.searchParams.delete("ownerKnowledge");
      window.history.replaceState({}, "", `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`);
    }
    void refresh().catch((cause) => setError(errorMessage(cause, "无法读取主人资料插件状态"))).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedRoleId) {
      setUnderstanding(null);
      return;
    }
    setLoadingUnderstanding(true);
    void api<{ understanding: OwnerUnderstanding | null }>(`/api/roles/${encodeURIComponent(selectedRoleId)}/owner-understanding`)
      .then((result) => setUnderstanding(result.understanding))
      .catch((cause) => setError(errorMessage(cause, "无法读取角色想法")))
      .finally(() => setLoadingUnderstanding(false));
  }, [selectedRoleId]);

  const run = async (action: "save" | "authorize" | "refresh" | "enable" | "pause" | "sync" | "disconnect" | "regenerate") => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      if (action === "save") {
        await api("/api/owner-knowledge/oauth-config", { method: "PUT", body: JSON.stringify({ appId, appSecret }) });
        setAppSecret("");
        setMessage("飞书应用配置已保存");
      } else if (action === "authorize") {
        const result = await api<{ authorizationUrl: string }>("/api/owner-knowledge/authorize", { method: "POST" });
        window.location.assign(result.authorizationUrl);
        return;
      } else if (action === "refresh") {
        await refresh();
        setMessage("飞书连接状态已刷新");
      } else if (action === "enable") {
        await api("/api/owner-knowledge/enable", { method: "POST" });
        setMessage("主人资料插件已启用并完成补同步");
      } else if (action === "pause") {
        await api("/api/owner-knowledge/pause", { method: "POST" });
        setMessage("主人资料插件已暂停，本地资料已保留");
      } else if (action === "sync") {
        const result = await api<{ report: SyncReport }>("/api/owner-knowledge/sync", { method: "POST" });
        setMessage(result.report.complete ? "同步完成" : "同步未完整完成，旧资料已保留");
      } else if (action === "regenerate") {
        const result = await api<{ understanding: OwnerUnderstanding | null }>(`/api/roles/${encodeURIComponent(selectedRoleId)}/owner-understanding/regenerate`, { method: "POST" });
        setUnderstanding(result.understanding);
        setMessage("角色想法已更新");
      } else {
        if (!window.confirm("断开后会删除飞书授权、本地资料索引和角色想法。飞书原文、会话和角色记忆不会删除。继续吗？")) return;
        await api("/api/owner-knowledge/disconnect", { method: "POST" });
        setMessage("飞书连接及本地资料已清除");
      }
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause, "主人资料操作失败"));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Placeholder loading>正在加载主人资料设置…</Placeholder>;

  const lastSync = status?.lastSync;
  const isConfigured = Boolean(status?.oauthConfigured);
  const syncFinishedAt = lastSync?.finishedAt ?? lastSync?.finished_at;
  const skippedDocuments = documents.filter((document) => document.status === "skipped");
  return <section className="owner-knowledge-settings page-narrow" aria-labelledby="owner-knowledge-title">
    <header className="owner-settings-header">
      <div className="owner-settings-title">
        <span className="owner-settings-icon"><Icon name="memory" size={20} /></span>
        <div><h2 id="owner-knowledge-title">主人资料</h2><p className="muted">飞书知识库</p></div>
      </div>
      <div className="owner-plugin-control">
        <span className={`owner-plugin-state${status?.enabled ? " is-enabled" : ""}`}><span aria-hidden="true" />{status?.enabled ? "运行中" : status?.connected ? "已暂停" : "未连接"}</span>
        <label className="plugin-switch" title={status?.connected ? "启用或暂停主人资料" : "连接飞书后可启用"}>
          <input type="checkbox" checked={Boolean(status?.enabled)} disabled={busy || !status?.connected} onChange={(event) => void run(event.target.checked ? "enable" : "pause")} aria-label="启用主人资料插件" />
          <span className="plugin-switch-track" aria-hidden="true"><span /></span>
        </label>
      </div>
    </header>

    {error && <Notice>{error}</Notice>}
    {message && <Notice tone="success">{message}</Notice>}
    {status?.lastError && <Notice>{status.lastError}</Notice>}

    <div className="owner-status-strip" aria-label="主人资料状态">
      <div><span>授权</span><strong>{status?.connected ? "已连接" : "待连接"}</strong></div>
      <div><span>语义模型</span><strong>{status?.embeddingReady ? "本机可用" : "未就绪"}</strong></div>
      <div><span>本地文档</span><strong>{documents.filter((document) => document.status === "synced").length}</strong></div>
      {lastSync && <div><span>最近同步</span><strong>{syncFinishedAt ? new Date(syncFinishedAt).toLocaleString() : lastSync.complete ? "已完成" : "未完成"}</strong></div>}
    </div>

    <section className="owner-plugin-section" aria-labelledby="feishu-connection-title">
      <div className="owner-section-heading"><div><h3 id="feishu-connection-title">飞书连接</h3><p className="muted">只读授权 · 整个知识库</p></div><span className={`owner-connection-badge${status?.connected ? " is-connected" : ""}`}><span aria-hidden="true" />{status?.connected ? "已授权" : "未授权"}</span></div>
      {isConfigured ? <>
        <div className="owner-configured-app"><div><span className="muted">应用 ID</span><strong>{appId || "由环境变量提供"}</strong></div><div><span className="muted">应用 Secret</span><strong>已安全保存</strong></div></div>
        <details className="owner-config-details"><summary>修改应用配置</summary><div className="owner-fields-grid"><div className="field"><label htmlFor="feishu-app-id">飞书应用 ID</label><input id="feishu-app-id" autoComplete="off" value={appId} disabled={busy} onChange={(event) => setAppId(event.target.value)} /></div><div className="field"><label htmlFor="feishu-app-secret">飞书应用 Secret</label><input id="feishu-app-secret" type="password" autoComplete="new-password" value={appSecret} placeholder="留空以保留现有 Secret" disabled={busy} onChange={(event) => setAppSecret(event.target.value)} /></div><button type="button" className="primary" disabled={busy || !appId.trim()} onClick={() => void run("save")}>保存配置</button></div></details>
      </> : <div className="owner-fields-grid"><div className="field"><label htmlFor="feishu-app-id">飞书应用 ID</label><input id="feishu-app-id" autoComplete="off" value={appId} disabled={busy} onChange={(event) => setAppId(event.target.value)} /></div><div className="field"><label htmlFor="feishu-app-secret">飞书应用 Secret</label><input id="feishu-app-secret" type="password" autoComplete="new-password" value={appSecret} disabled={busy} onChange={(event) => setAppSecret(event.target.value)} /></div><button type="button" className="primary" disabled={busy || !appId.trim() || !appSecret.trim()} onClick={() => void run("save")}>保存应用配置</button></div>}
      <div className="owner-connection-footer"><div className="owner-redirect"><span className="muted">授权回调地址</span><code>{status?.redirectUri}</code></div><div className="owner-actions">{isConfigured && <><button type="button" className="primary" disabled={busy} onClick={() => void run("authorize")}>{status?.connected ? "重新授权" : "连接飞书"}</button><button type="button" disabled={busy} onClick={() => void run("refresh")}>刷新状态</button></>}{status?.connected && <button type="button" className="danger" disabled={busy} onClick={() => void run("disconnect")}>断开并清除资料</button>}</div></div>
    </section>

    <section className="owner-plugin-section" aria-labelledby="sync-title">
      <div className="owner-section-heading"><div><h3 id="sync-title">知识库同步</h3><p className="muted">自动每日检查 · 启动时补同步</p></div><button type="button" disabled={busy || !status?.enabled} onClick={() => void run("sync")}>{busy ? "处理中…" : "立即同步"}</button></div>
      {lastSync ? <><div className="owner-sync-heading"><span className={`owner-sync-result${lastSync.complete ? " is-complete" : ""}`}><span aria-hidden="true" />{lastSync.complete ? "扫描完成" : "扫描未完成"}</span>{syncFinishedAt && <time dateTime={syncFinishedAt}>{new Date(syncFinishedAt).toLocaleString()}</time>}</div><dl className="owner-sync-summary" aria-label="最近一次同步结果"><div><dt>新增</dt><dd>{lastSync.created}</dd></div><div><dt>更新</dt><dd>{lastSync.updated}</dd></div><div><dt>删除</dt><dd>{lastSync.deleted}</dd></div><div><dt>跳过</dt><dd>{lastSync.skipped}</dd></div><div><dt>失败</dt><dd>{lastSync.failed}</dd></div><div><dt>未变化</dt><dd>{lastSync.unchanged}</dd></div></dl></> : <p className="owner-empty-line">尚无同步记录</p>}
      {lastSync?.errors?.length ? <ul className="owner-sync-errors">{lastSync.errors.slice(0, 10).map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : null}
      {skippedDocuments.length > 0 && <details className="owner-skipped-documents"><summary>已跳过的节点 <span>{skippedDocuments.length}</span></summary><ul>{skippedDocuments.slice(0, 20).map((document) => <li key={document.documentId}><a href={document.url} target="_blank" rel="noreferrer">{document.title}</a><span> · {document.objectType}</span></li>)}</ul></details>}
    </section>

    <section className="owner-plugin-section" aria-labelledby="understanding-title">
      <div className="owner-section-heading"><div><h3 id="understanding-title">角色对主人的想法</h3><p className="muted">按角色分别保存</p></div>{roles.length > 0 && <select aria-label="选择角色" value={selectedRoleId} onChange={(event) => setSelectedRoleId(event.target.value)}>{roles.map((role) => <option value={role.id} key={role.id}>{role.name}</option>)}</select>}</div>
      {roles.length === 0 ? <p className="owner-empty-line">尚无角色</p> : loadingUnderstanding ? <Placeholder loading>正在读取…</Placeholder> : understanding ? <div className="owner-understanding" aria-label="角色想法"><p>{understanding.body}</p><small className="muted">更新于 {new Date(understanding.updatedAt).toLocaleString()}</small></div> : <p className="owner-empty-line">该角色还没有生成想法</p>}
      <div className="owner-actions"><button type="button" disabled={busy || !status?.enabled || !selectedRoleId} onClick={() => void run("regenerate")}>{busy ? "处理中…" : "重新生成想法"}</button></div>
    </section>
  </section>;
}
