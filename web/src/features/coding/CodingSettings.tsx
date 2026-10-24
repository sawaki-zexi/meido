import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import { getCodingConfiguration, listCodingProbes, startCodingProbe, saveCodingBuild, saveCodingProfile, saveCodingRepository, setCodingEnabled, type CodingConfiguration, type CodingProbe } from "../../api/coding";
import type { Role } from "../../api/types";
import { Notice } from "../../ui/Status";
import { CodingPublicationCredentials } from "./CodingPublicationCredentials";

type RepositoryDraft = { id?: string; root: string; baseRef: string; remote: string; validation: string; localCommit: boolean; push: boolean; draftPr: boolean };
type ProfileDraft = { id?: string; name: string; kind: "codex" | "claude"; executor: "herdr" | "codex_exec"; executable: string; sha256: string; version: string; model: string; authDirectory: string };

export function CodingSettings({ showTitle = true }: { showTitle?: boolean }) {
  const editor = useRef<HTMLDetailsElement>(null);
  const [configuration, setConfiguration] = useState<CodingConfiguration | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [probes, setProbes] = useState<CodingProbe[]>([]);
  const [probeConsent, setProbeConsent] = useState(false);
  const [repo, setRepo] = useState<RepositoryDraft>({ root: "", baseRef: "main", remote: "origin", validation: "[]", localCommit: false, push: false, draftPr: false });
  const [build, setBuild] = useState({ executable: "", sha256: "" });
  const [profile, setProfile] = useState<ProfileDraft>({ name: "", kind: "codex", executor: "herdr", executable: "", sha256: "", version: "", model: "", authDirectory: "" });
  const refresh = async () => { const [value, data, probeData] = await Promise.all([getCodingConfiguration(), api<{ roles: Role[] }>("/api/roles"), listCodingProbes()]); setConfiguration(value); setRoles(data.roles); setProbes(probeData.probes); };
  useEffect(() => { void refresh().catch(cause => setError(errorMessage(cause, "无法读取编程配置"))); }, []);
  const perform = async (operation: () => Promise<unknown>, success: string) => { setBusy(true); setError(""); setMessage(""); try { await operation(); await refresh(); setMessage(success); } catch (cause) { setError(errorMessage(cause, "保存失败")); } finally { setBusy(false); } };
  const saveRepo = (event: FormEvent) => { event.preventDefault(); void perform(() => saveCodingRepository({ ...repo, validation: JSON.parse(repo.validation), allowedRoleIds: allowed }), "可信仓库已登记；还需在插件设置中为角色选择授权。"); };
  const saveProfile = (event: FormEvent) => { event.preventDefault(); void perform(() => saveCodingProfile({ ...profile, allowedRoleIds: allowed, configurationManifest: [] }), "CLI 已登记，尚未核验。保存不会登录或调用模型。"); };
  return <section className="coding-settings" aria-label="编程委托设置">
    <div className="settings-heading"><div>{showTitle && <h2>编程委托</h2>}<span className="muted">{configuration ? configuration.enabled ? '已启用' : '已关闭' : '正在读取'}</span></div>
      <button type="button" disabled={busy || !configuration} onClick={() => void perform(() => setCodingEnabled(!configuration!.enabled), "全局设置已保存")}>{configuration?.enabled ? '关闭编程委托' : '启动编程委托'}</button></div>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    <p>仅授权可信仓库；程序使用当前账号权限，运行可能消耗模型额度。</p>
    <details className="settings-disclosure" ref={editor}><summary>仓库与 CLI 配置</summary><div>
    <fieldset disabled={busy}><legend>授予这些角色</legend>{roles.map(role => <label className="checkbox-field" key={role.id}><input type="checkbox" checked={allowed.includes(role.id)} onChange={event => setAllowed(event.target.checked ? [...allowed, role.id] : allowed.filter(id => id !== role.id))} />{role.name}</label>)}</fieldset>
    <form onSubmit={saveRepo}><fieldset disabled={busy}><legend>登记可信仓库</legend>
      <label className="field">仓库绝对路径<input required aria-label="可信仓库路径" value={repo.root} onChange={event => setRepo({ ...repo, root: event.target.value })} /></label>
      <label className="field">起始分支<input required value={repo.baseRef} onChange={event => setRepo({ ...repo, baseRef: event.target.value })} /></label>
      <label className="field">发布 remote 名称<input required value={repo.remote} onChange={event => setRepo({ ...repo, remote: event.target.value })} /></label>
      {([["localCommit", "允许宿主本地提交"], ["push", "允许批准后推送"], ["draftPr", "允许批准后创建 Draft PR"]] as const).map(([key, label]) => <label className="checkbox-field" key={key}><input type="checkbox" checked={repo[key]} onChange={event => setRepo({ ...repo, [key]: event.target.checked })} />{label}</label>)}
      <small>提交与发布仍需逐次批准；remote 须为无凭据的 GitHub HTTPS 地址。</small>
      <label className="field">固定验证命令<textarea aria-label="固定验证命令" required value={repo.validation} onChange={event => setRepo({ ...repo, validation: event.target.value })} /></label><small>填写 JSON 数组，每项含 id、argv 和相对 cwd；argv 首项为固定程序绝对路径。验证会执行该仓库的代码。</small>
      <button type="submit">登记仓库</button>
    </fieldset></form>
    <form onSubmit={event => { event.preventDefault(); void perform(() => saveCodingBuild(build.executable, build.sha256), "owned 宿主及资源已核验"); }}><fieldset disabled={busy}><legend>固定 owned Herdr 构建</legend><label className="field">程序路径<input aria-label="owned Herdr 路径" required value={build.executable} onChange={event => setBuild({ ...build, executable: event.target.value })} /></label><label className="field">SHA-256<input required value={build.sha256} onChange={event => setBuild({ ...build, sha256: event.target.value })} /></label><button type="submit">核验宿主</button></fieldset></form>
    <form onSubmit={saveProfile}><fieldset disabled={busy}><legend>登记专用 CLI</legend>
      <label className="field">名称<input required value={profile.name} onChange={event => setProfile({ ...profile, name: event.target.value })} /></label>
      <label className="field">委托方式<select value={profile.executor} onChange={event => setProfile({ ...profile, executor: event.target.value as "herdr" | "codex_exec", kind: event.target.value === "codex_exec" ? "codex" : profile.kind })}><option value="herdr">Herdr</option><option value="codex_exec">Codex exec（codex-delegate）</option></select></label>
      <label className="field">CLI<select value={profile.kind} disabled={profile.executor === "codex_exec"} onChange={event => setProfile({ ...profile, kind: event.target.value as "codex" | "claude" })}><option value="codex">Codex</option><option value="claude">Claude Code</option></select></label>
      {([['executable', '程序绝对路径'], ['sha256', '程序 SHA-256'], ['version', '固定版本'], ['model', '固定模型'], ['authDirectory', '专用认证目录']] as const).map(([key, label]) => <label className="field" key={key}>{label}<input aria-label={label} required value={profile[key]} onChange={event => setProfile({ ...profile, [key]: event.target.value })} /></label>)}
      <button type="submit">登记 CLI</button>
    </fieldset></form>
    </div></details>
    <details className="settings-disclosure"><summary>发布凭据</summary><div><CodingPublicationCredentials /></div></details>
    <h3>已登记</h3>{configuration?.repositories.map(item => <div key={item.id}><p>{item.root} · {item.baseRef}</p><button type="button" disabled={busy} onClick={() => { if (editor.current) editor.current.open = true; setRepo({ id: item.id, root: item.root, baseRef: item.baseRef, remote: item.remote, validation: JSON.stringify(item.validation, null, 2), localCommit: item.localCommit ?? false, push: item.push ?? false, draftPr: item.draftPr ?? false }); setAllowed(item.allowedRoleIds); }}>编辑仓库 {item.root}</button><button type="button" disabled={busy} onClick={() => void perform(() => saveCodingRepository({ id: item.id, root: item.root, baseRef: item.baseRef, remote: item.remote, validation: item.validation, allowedRoleIds: [] }), "已撤销仓库的角色许可并停止当前任务")}>撤销仓库 {item.root}</button></div>)}{configuration?.profiles.map(item => <div key={item.id}><p>{item.name} · {item.verification.status === "verified" ? "已核验" : item.verification.reason ?? "尚未核验"}</p><button type="button" disabled={busy} onClick={() => { if (editor.current) editor.current.open = true; setProfile({ id: item.id, name: item.name, kind: item.kind, executor: item.executor, executable: item.executable, sha256: item.sha256, version: item.version, model: item.model, authDirectory: item.authDirectory }); setAllowed(item.allowedRoleIds); }}>编辑 CLI {item.name}</button><button type="button" disabled={busy} onClick={() => void perform(() => saveCodingProfile({ id: item.id, name: item.name, kind: item.kind, executor: item.executor, executable: item.executable, sha256: item.sha256, version: item.version, model: item.model, authDirectory: item.authDirectory, configurationManifest: item.configurationManifest, allowedRoleIds: [] }), "已撤销 CLI 的角色许可并停止当前任务")}>撤销 CLI {item.name}</button></div>)}
    <details className="settings-disclosure"><summary>原生核验</summary><div><p>核验最多运行 10 分钟，可能消耗模型额度。需要 Codex 0.159.3 或 Claude Code 2.1.283。</p>
    <label className="checkbox-field"><input type="checkbox" checked={probeConsent} onChange={event => setProbeConsent(event.target.checked)} />我批准所选 CLI 执行首次和继续两个任务回合，期间可能多次请求模型</label>
    {configuration?.profiles.map(item => <div key={item.id}><span>{item.name} · {item.version} · {item.model}</span><button type="button" disabled={busy || !probeConsent || (item.executor === "herdr" && !configuration.nativeBuild) || !item.allowedRoleIds.length} onClick={() => { const digest = item.executor === "codex_exec" ? item.sha256 : configuration.nativeBuild?.sha256; if (digest) void perform(() => startCodingProbe(item, item.allowedRoleIds[0], digest, crypto.randomUUID()), "原生核验已启动；请刷新查看确切结果。").then(() => setProbeConsent(false)); }}>核验 {item.name}</button></div>)}
    <button type="button" disabled={busy} onClick={() => void perform(refresh, "状态已刷新")}>刷新核验状态</button>
    {probes.map(item => <p key={item.id}>{configuration?.profiles.find(profile => profile.id === item.profileId)?.name ?? item.profileId} · {item.status} · {item.reason} · 资源：{item.cleanupState ?? "未启动"}</p>)}
    </div></details>
  </section>;
}
