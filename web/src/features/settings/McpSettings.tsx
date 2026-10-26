import { useEffect, useState, type FormEvent } from "react";
import { errorMessage } from "../../api/client";
import { deleteMcpServer, listMcpRoles, listMcpServers, retryMcpCleanup, saveMcpServer, type McpRisk, type McpServer } from "../../api/mcp";
import type { Role } from "../../api/types";
import { Notice, Placeholder } from "../../ui/Status";
import "./mcp-settings.css";

type Environment = { key: string; value: string };
type Draft = {
  serverId: string; name: string; executable: string; arguments: string; cwd: string; env: Environment[];
  roleIds: string[]; enabled: boolean; risk: McpRisk; timeoutSeconds: number; maxOutputChars: number; enableDiscovery: boolean;
};
const emptyDraft = (): Draft => ({ serverId: "", name: "", executable: "", arguments: "", cwd: "", env: [], roleIds: [], enabled: true, risk: "external", timeoutSeconds: 30, maxOutputChars: 20000, enableDiscovery: true });
const riskLabels: Record<McpRisk, string> = { read_only: "只读", mutating: "修改数据", external: "外部事务" };
const discoveryTools = ["tool_search", "mcp_add", "mcp_list", "mcp_remove"];

export function McpSettings() {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [readFailed, setReadFailed] = useState(false);

  const refresh = async () => {
    try {
      const [catalog, roleData] = await Promise.all([listMcpServers(), listMcpRoles()]);
      setServers(catalog.servers); setRoles(roleData.roles); setReadFailed(false);
    } catch (cause) { setReadFailed(true); throw cause; }
  };
  const reload = async () => {
    setBusy(true); setError("");
    try { await refresh(); }
    catch (cause) { setError(errorMessage(cause, "无法读取 MCP 配置")); }
    finally { setBusy(false); }
  };
  useEffect(() => { void reload(); }, []);

  const change = (patch: Partial<Draft>) => setDraft((current) => current ? { ...current, ...patch } : current);
  const updateEnv = (index: number, patch: Partial<Environment>) => {
    if (draft) change({ env: draft.env.map((item, position) => position === index ? { ...item, ...patch } : item) });
  };
  const edit = (server: McpServer) => {
    setEditingId(server.serverId);
    setDraft({ serverId: server.serverId, name: server.name, executable: server.command[0], arguments: server.command.slice(1).join("\n"), cwd: server.cwd,
      env: server.envKeys.map((key) => ({ key, value: "" })), roleIds: [...server.roleIds], enabled: server.enabled, risk: server.risk,
      timeoutSeconds: server.timeoutSeconds, maxOutputChars: server.maxOutputChars, enableDiscovery: true });
    setError(""); setMessage("");
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft) return;
    if (!draft.name.trim()) { setError("请填写服务名称"); return; }
    if (new Set(draft.env.map((item) => item.key)).size !== draft.env.length) { setError("环境变量名称不能重复"); return; }
    setBusy(true); setError(""); setMessage("");
    try {
      await saveMcpServer({
        serverId: draft.serverId, name: draft.name.trim(), command: [draft.executable, ...(draft.arguments ? draft.arguments.split(/\r?\n/) : [])], cwd: draft.cwd,
        env: Object.fromEntries(draft.env.map((item) => [item.key, item.value])), roleIds: draft.roleIds, enabled: draft.enabled, risk: draft.risk,
        timeoutSeconds: draft.timeoutSeconds, maxOutputChars: draft.maxOutputChars, enableDiscovery: draft.enableDiscovery,
      }, editingId !== null);
      setDraft(null);
      setMessage(editingId ? "MCP 配置已保存；连接已撤销，请在对话中重新连接。" : "MCP 配置已保存；尚未启动服务。");
      await refresh();
    } catch (cause) { setError(errorMessage(cause, "MCP 配置保存失败")); }
    finally { setBusy(false); }
  };
  const revoke = async (server: McpServer) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await saveMcpServer({ serverId: server.serverId, name: server.name, command: server.command, cwd: server.cwd, env: Object.fromEntries(server.envKeys.map((key) => [key, ""])),
        roleIds: [], enabled: server.enabled, risk: server.risk, timeoutSeconds: server.timeoutSeconds, maxOutputChars: server.maxOutputChars, enableDiscovery: false }, true);
      if (editingId === server.serverId) setDraft(null);
      setMessage("已撤销该服务的全部角色授权并断开连接。");
      await refresh();
    } catch (cause) { setError(errorMessage(cause, "撤销 MCP 授权失败")); }
    finally { setBusy(false); }
  };
  const remove = async (server: McpServer) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await deleteMcpServer(server.serverId);
      if (editingId === server.serverId) setDraft(null);
      setDeleteId(null); setMessage("MCP 服务及凭据已删除。");
      await refresh();
    } catch (cause) { setError(errorMessage(cause, "删除 MCP 服务失败")); }
    finally { setBusy(false); }
  };

  const retryCleanup = async (server: McpServer) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await retryMcpCleanup(server.serverId);
      setMessage("MCP 待清理资源已回收；未重新执行工具。");
      await refresh();
    } catch (cause) { setError(errorMessage(cause, "MCP 资源清理尚未确认；请重试清理")); }
    finally { setBusy(false); }
  };

  const savedEnvKeys = servers?.find((server) => server.serverId === editingId)?.envKeys ?? [];
  const missingDiscovery = draft && !draft.enableDiscovery ? roles.filter((role) => draft.roleIds.includes(role.id)).map((role) => ({ role, missing: discoveryTools.filter((name) => !role.agentConfig?.enabledTools?.includes(name)) })).filter((item) => item.missing.length > 0) : [];
  return <section className="mcp-settings" aria-labelledby="mcp-settings-title">
    <header className="mcp-heading"><h2 id="mcp-settings-title">MCP 服务</h2><div className="mcp-actions"><button type="button" disabled={busy} onClick={() => void reload()}>刷新状态</button><button type="button" disabled={busy || servers === null} onClick={() => { setEditingId(null); setDraft(emptyDraft()); setError(""); setMessage(""); }}>添加 MCP 服务</button></div></header>
    {error && <Notice>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    {readFailed && servers !== null && <p role="status">状态读取失败；下方为上次读取结果。</p>}
    {servers === null && !error && <Placeholder loading>正在读取 MCP 服务…</Placeholder>}
    {servers?.length === 0 && <p className="muted">尚未配置 MCP 服务。</p>}
    {servers?.map((server) => <section className="mcp-card" aria-label={`服务 ${server.name}`} key={server.serverId}>
      <header className="mcp-heading"><div><h3>{server.name}</h3><small>{server.serverId} · {server.enabled ? "配置已启用" : "已停用"} · {riskLabels[server.risk]}</small></div><button type="button" aria-label={`编辑 ${server.name}`} disabled={busy} onClick={() => edit(server)}>编辑</button></header>
      <div className="mcp-state"><span>运行中连接：{server.activeProcesses}</span><span>已保存连接意愿：{server.connectionCount}</span></div>
      {!!server.cleanupOwners?.length && <div>
        <p>待确认清理：{server.cleanupOwners.length}</p>
        <p>原运行：{server.cleanupOwners.map((owner) => owner.runId).join("、")}</p>
        <button type="button" aria-label={`重试清理 ${server.name}`} disabled={busy} onClick={() => void retryCleanup(server)}>重试清理</button>
      </div>}
      <p>授权角色：{server.roleIds.map((id) => roles.find((role) => role.id === id)?.name ?? "已删除角色").join("、") || "无"}</p>
      <p>环境变量：{server.envKeys.join("、") || "无"}</p>
      {server.errors.map((item) => <Notice key={item}>{item}</Notice>)}
      <div className="mcp-actions"><button type="button" aria-label={`撤销 ${server.name} 的全部角色授权`} disabled={busy || server.roleIds.length === 0} onClick={() => void revoke(server)}>撤销全部角色授权</button><button type="button" aria-label={`删除 ${server.name}`} disabled={busy} onClick={() => setDeleteId(server.serverId)}>删除服务</button></div>
      {deleteId === server.serverId && <div className="mcp-delete"><p>删除后将断开所有连接并清除配置和凭据。</p><div className="mcp-actions"><button type="button" disabled={busy} onClick={() => setDeleteId(null)}>取消删除</button><button type="button" className="danger" aria-label={`确认删除 ${server.name}`} disabled={busy} onClick={() => void remove(server)}>确认删除</button></div></div>}
    </section>)}
    {draft && <form className="mcp-card mcp-form" onSubmit={(event) => void save(event)} aria-label="MCP 配置">
      <h3>{editingId ? "编辑服务" : "添加服务"}</h3>
      <fieldset disabled={busy}>
        <div className="mcp-grid">
          <div className="field"><label htmlFor="mcp-id">服务 ID</label><input id="mcp-id" required disabled={editingId !== null} pattern="[a-z][a-z0-9_]{0,23}" maxLength={24} title="小写字母开头，最多 24 位小写字母、数字或下划线" value={draft.serverId} onChange={(event) => change({ serverId: event.target.value })} /></div>
          <div className="field"><label htmlFor="mcp-name">服务名称</label><input id="mcp-name" required maxLength={100} value={draft.name} onChange={(event) => change({ name: event.target.value })} /></div>
        </div>
        <div className="field"><label htmlFor="mcp-executable">可执行文件（绝对路径）</label><input id="mcp-executable" required value={draft.executable} onChange={(event) => change({ executable: event.target.value })} /><small className="muted">填写后端所在电脑上已有的可执行文件。</small></div>
        <div className="field"><label htmlFor="mcp-arguments">参数（每行一个）</label><textarea id="mcp-arguments" rows={3} value={draft.arguments} onChange={(event) => change({ arguments: event.target.value })} /><small className="muted">路径含空格也无需引号。</small></div>
        <div className="field"><label htmlFor="mcp-cwd">工作目录（绝对路径）</label><input id="mcp-cwd" required value={draft.cwd} onChange={(event) => change({ cwd: event.target.value })} /></div>
        <div className="mcp-environment"><h4>环境变量</h4><p className="muted">已有值不回显；留空保留，移除即删除。</p>
          {draft.env.map((item, index) => <div className="mcp-env-row" key={index}>
            <div className="field"><label htmlFor={`mcp-env-key-${index}`}>环境变量名称 {index + 1}</label><input id={`mcp-env-key-${index}`} required pattern="[A-Za-z_][A-Za-z0-9_]{0,127}" value={item.key} onChange={(event) => updateEnv(index, { key: event.target.value })} /></div>
            <div className="field"><label htmlFor={`mcp-env-value-${index}`}>环境变量值 {index + 1}</label><input id={`mcp-env-value-${index}`} type="password" autoComplete="new-password" required={!savedEnvKeys.includes(item.key)} placeholder={savedEnvKeys.includes(item.key) ? "已配置；留空保留原值" : "填写新变量值"} value={item.value} onChange={(event) => updateEnv(index, { value: event.target.value })} /></div>
            <button type="button" aria-label={`移除环境变量 ${index + 1}`} onClick={() => change({ env: draft.env.filter((_, position) => position !== index) })}>移除</button>
          </div>)}
          <button type="button" disabled={draft.env.length >= 64} onClick={() => change({ env: [...draft.env, { key: "", value: "" }] })}>添加环境变量</button>
        </div>
        <fieldset className="mcp-grants"><legend>角色授权</legend>{roles.length === 0 && <p className="muted">请先创建角色，再为服务授权。</p>}{roles.map((role) => <label className="mcp-checkbox" key={role.id}><input type="checkbox" aria-label={`授权 ${role.name}`} checked={draft.roleIds.includes(role.id)} onChange={(event) => change({ roleIds: event.target.checked ? [...draft.roleIds, role.id] : draft.roleIds.filter((id) => id !== role.id) })} /><span>{role.name}</span></label>)}
          {draft.roleIds.filter((id) => !roles.some((role) => role.id === id)).map((id) => <label className="mcp-checkbox" key={id}><input type="checkbox" aria-label={`移除已删除角色 ${id} 的授权`} checked onChange={() => change({ roleIds: draft.roleIds.filter((value) => value !== id) })} /><span>已删除角色（{id}），取消勾选以移除授权</span></label>)}
        </fieldset>
        <label className="mcp-checkbox"><input type="checkbox" checked={draft.enableDiscovery} onChange={(event) => change({ enableDiscovery: event.target.checked })} /><span>同时为授权角色开启 tool_search 和 mcp_add、mcp_list、mcp_remove</span></label>
        {!draft.enableDiscovery && <p className="muted">不会撤销已有工具权限。</p>}
        {missingDiscovery.map(({ role, missing }) => <Notice key={role.id}>{`${role.name} 尚未开启 ${missing.join("、")}；需在角色设置中开启后才能使用对应的发现或连接管理操作。`}</Notice>)}
        <div className="mcp-grid">
          <div className="field"><label htmlFor="mcp-risk">远端工具风险</label><select id="mcp-risk" value={draft.risk} onChange={(event) => change({ risk: event.target.value as McpRisk })}>{Object.entries(riskLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div>
          <div className="field"><label htmlFor="mcp-timeout">超时（秒）</label><input id="mcp-timeout" type="number" required min={1} max={120} step="any" value={draft.timeoutSeconds} onChange={(event) => change({ timeoutSeconds: Number(event.target.value) })} /></div>
          <div className="field"><label htmlFor="mcp-output">输出字符上限</label><input id="mcp-output" type="number" required min={1024} max={200000} value={draft.maxOutputChars} onChange={(event) => change({ maxOutputChars: Number(event.target.value) })} /></div>
        </div>
        <label className="mcp-checkbox"><input type="checkbox" checked={draft.enabled} onChange={(event) => change({ enabled: event.target.checked })} /><span>启用此服务授权</span></label>
        <p className="muted">保存不启动服务，由角色按需连接。</p>
        {editingId && <p className="muted">修改后断开现有连接，需重新连接。</p>}
        <div className="form-actions"><button type="button" onClick={() => setDraft(null)}>取消</button><button type="submit" className="primary">{busy ? "保存中…" : "保存 MCP 配置"}</button></div>
      </fieldset>
    </form>}
  </section>;
}
