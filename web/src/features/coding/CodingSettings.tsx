import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import { getCodingConfiguration, listCodingProbes, startCodingProbe, saveCodingBuild, saveCodingProfile, saveCodingRepository, setCodingEnabled, type CodingConfiguration, type CodingProbe } from "../../api/coding";
import type { Role } from "../../api/types";

type RepositoryDraft = { id?: string; root: string; baseRef: string; remote: string; validation: string };
type ProfileDraft = { id?: string; name: string; kind: "codex" | "claude"; executable: string; sha256: string; version: string; model: string; authDirectory: string };

export function CodingSettings() {
  const [configuration, setConfiguration] = useState<CodingConfiguration | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [probes, setProbes] = useState<CodingProbe[]>([]);
  const [probeConsent, setProbeConsent] = useState(false);
  const [repo, setRepo] = useState<RepositoryDraft>({ root: "", baseRef: "main", remote: "origin", validation: "[]" });
  const [build, setBuild] = useState({ executable: "", sha256: "" });
  const [profile, setProfile] = useState<ProfileDraft>({ name: "", kind: "codex", executable: "", sha256: "", version: "", model: "", authDirectory: "" });
  const refresh = async () => { const [value, data, probeData] = await Promise.all([getCodingConfiguration(), api<{ roles: Role[] }>("/api/roles"), listCodingProbes()]); setConfiguration(value); setRoles(data.roles); setProbes(probeData.probes); };
  useEffect(() => { void refresh().catch(cause => setError(errorMessage(cause, "无法读取编程配置"))); }, []);
  const perform = async (operation: () => Promise<unknown>, success: string) => { setBusy(true); setError(""); setMessage(""); try { await operation(); await refresh(); setMessage(success); } catch (cause) { setError(errorMessage(cause, "保存失败")); } finally { setBusy(false); } };
  const saveRepo = (event: FormEvent) => { event.preventDefault(); void perform(() => saveCodingRepository({ ...repo, validation: JSON.parse(repo.validation), allowedRoleIds: allowed }), "可信仓库已登记；还需在角色资料中选择授权。"); };
  const saveProfile = (event: FormEvent) => { event.preventDefault(); void perform(() => saveCodingProfile({ ...profile, executor: "herdr", allowedRoleIds: allowed, configurationManifest: [] }), "CLI 已登记，尚未核验。保存不会登录或调用模型。"); };
  return <section aria-label="编程委托设置"><h2>编程委托</h2>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <p>仅对信任的仓库开放。CLI 使用专门指定的认证目录；运行任务可能消耗所选账号的模型配额。发布需另行批准具体成果。</p>
    <label className="checkbox-field"><input aria-label="全局启用编程委托" type="checkbox" checked={configuration?.enabled ?? false} disabled={busy || !configuration} onChange={event => void perform(() => setCodingEnabled(event.target.checked), "全局设置已保存")} />启用编程委托</label>
    <fieldset disabled={busy}><legend>授予这些角色</legend>{roles.map(role => <label className="checkbox-field" key={role.id}><input type="checkbox" checked={allowed.includes(role.id)} onChange={event => setAllowed(event.target.checked ? [...allowed, role.id] : allowed.filter(id => id !== role.id))} />{role.name}</label>)}</fieldset>
    <form onSubmit={saveRepo}><fieldset disabled={busy}><legend>登记可信仓库</legend>
      <label className="field">仓库绝对路径<input required aria-label="可信仓库路径" value={repo.root} onChange={event => setRepo({ ...repo, root: event.target.value })} /></label>
      <label className="field">起始分支<input required value={repo.baseRef} onChange={event => setRepo({ ...repo, baseRef: event.target.value })} /></label>
      <label className="field">固定验证命令<textarea aria-label="固定验证命令" required value={repo.validation} onChange={event => setRepo({ ...repo, validation: event.target.value })} /></label><small>填写 JSON 数组，每项含 id、argv 和相对 cwd；argv 首项为固定程序绝对路径。验证会执行该仓库的代码。</small>
      <button type="submit">登记仓库</button>
    </fieldset></form>
    <form onSubmit={event => { event.preventDefault(); void perform(() => saveCodingBuild(build.executable, build.sha256), "owned 宿主及资源已核验"); }}><fieldset disabled={busy}><legend>固定 owned Herdr 构建</legend><label className="field">程序路径<input aria-label="owned Herdr 路径" required value={build.executable} onChange={event => setBuild({ ...build, executable: event.target.value })} /></label><label className="field">SHA-256<input required value={build.sha256} onChange={event => setBuild({ ...build, sha256: event.target.value })} /></label><button type="submit">核验宿主</button></fieldset></form>
    <form onSubmit={saveProfile}><fieldset disabled={busy}><legend>登记专用 CLI</legend>
      <label className="field">名称<input required value={profile.name} onChange={event => setProfile({ ...profile, name: event.target.value })} /></label>
      <label className="field">CLI<select value={profile.kind} onChange={event => setProfile({ ...profile, kind: event.target.value as "codex" | "claude" })}><option value="codex">Codex</option><option value="claude">Claude Code</option></select></label>
      {([['executable', '程序绝对路径'], ['sha256', '程序 SHA-256'], ['version', '固定版本'], ['model', '固定模型'], ['authDirectory', '专用认证目录']] as const).map(([key, label]) => <label className="field" key={key}>{label}<input aria-label={label} required value={profile[key]} onChange={event => setProfile({ ...profile, [key]: event.target.value })} /></label>)}
      <button type="submit">登记 CLI</button>
    </fieldset></form>
    <h3>已登记</h3>{configuration?.repositories.map(item => <div key={item.id}><p>{item.root} · {item.baseRef}</p><button type="button" disabled={busy} onClick={() => { setRepo({ id: item.id, root: item.root, baseRef: item.baseRef, remote: item.remote, validation: JSON.stringify(item.validation, null, 2) }); setAllowed(item.allowedRoleIds); }}>编辑仓库 {item.root}</button><button type="button" disabled={busy} onClick={() => void perform(() => saveCodingRepository({ id: item.id, root: item.root, baseRef: item.baseRef, remote: item.remote, validation: item.validation, allowedRoleIds: [] }), "已撤销仓库的角色许可并停止当前任务")}>撤销仓库 {item.root}</button></div>)}{configuration?.profiles.map(item => <div key={item.id}><p>{item.name} · {item.verification.status === "verified" ? "已核验" : item.verification.reason ?? "尚未核验"}</p><button type="button" disabled={busy} onClick={() => { setProfile({ id: item.id, name: item.name, kind: item.kind, executable: item.executable, sha256: item.sha256, version: item.version, model: item.model, authDirectory: item.authDirectory }); setAllowed(item.allowedRoleIds); }}>编辑 CLI {item.name}</button><button type="button" disabled={busy} onClick={() => void perform(() => saveCodingProfile({ id: item.id, name: item.name, kind: item.kind, executor: item.executor, executable: item.executable, sha256: item.sha256, version: item.version, model: item.model, authDirectory: item.authDirectory, configurationManifest: item.configurationManifest, allowedRoleIds: [] }), "已撤销 CLI 的角色许可并停止当前任务")}>撤销 CLI {item.name}</button></div>)}
    <h3>原生核验</h3><p>核验会使用所选专用认证与固定模型，在独立临时仓库执行首次和精确继续各一次，内部可能多次请求模型；最多运行 600 秒并可能消耗账号额度。需要 Codex 0.159.3 或 Claude Code 2.1.283。缺少原生拒绝证据将保持不可用。</p>
    <label className="checkbox-field"><input type="checkbox" checked={probeConsent} onChange={event => setProbeConsent(event.target.checked)} />我批准所选 CLI 执行首次和继续两个任务回合，期间可能多次请求模型</label>
    {configuration?.profiles.map(item => <div key={item.id}><span>{item.name} · {item.version} · {item.model}</span><button type="button" disabled={busy || !probeConsent || !configuration.nativeBuild || !item.allowedRoleIds.length} onClick={() => { const build = configuration.nativeBuild; if (build) void perform(() => startCodingProbe(item, item.allowedRoleIds[0], build.sha256, crypto.randomUUID()), "原生核验已启动；请刷新查看确切结果。").then(() => setProbeConsent(false)); }}>核验 {item.name}</button></div>)}
    <button type="button" disabled={busy} onClick={() => void perform(refresh, "状态已刷新")}>刷新核验状态</button>
    {probes.map(item => <p key={item.id}>{configuration?.profiles.find(profile => profile.id === item.profileId)?.name ?? item.profileId} · {item.status} · {item.reason} · 资源：{item.cleanupState ?? "未启动"}</p>)}
  </section>;
}
