import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Icon } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";

type MailStatus = {
  host: string; port: number; username: string; useSsl: boolean; enabled: boolean; paused: boolean;
  verified: boolean; autoDraftReady: boolean; folders: { inbox: string; sent: string; drafts: string };
  lastError: string | null; lastSyncAt: string | null; items: number; drafts: number;
  candidate?: { host: string; port: number; username: string; connection_tested: boolean; test_draft_present: boolean; test_sent: boolean } | null;
  testDraft?: { status: string; created_at: string; sent_at?: string; error?: string } | null;
  lastErrors: string[];
};
type MailItem = { id: string; threadId: string; sender: string; subject: string; receivedAt: string; status: string; needsReply: boolean | null; draftId: string | null; lastError: string | null; draft?: { id: string; status: string; stale: boolean; remoteMissing: boolean; recipients: string } };
type MailDetail = {
  item: { id: string; threadId: string; sender: string; replyTo: string; subject: string; receivedAt: string; body: string; status: string; lastError: string | null };
  thread: { id: string; sender: string; subject: string; receivedAt: string; body: string; status: string }[];
  drafts: { id: string; status: string; stale: boolean; remoteMissing: boolean; body: string; subject: string; recipients: string; updatedAt: string }[];
};
type Role = { id: string; name: string };
type MailJob = { jobId: string; status: "queued" | "running" | "completed" | "failed"; error: string | null; result: unknown };

const labels: Record<string, string> = { new: "待处理", no_reply: "无需回复", queued: "排队中", processing: "处理中", drafted: "草稿已创建", failed: "失败", uncertain: "待核实", created: "QQ 草稿", manual_modified: "主人已修改", stale: "可能过时", missing: "已不在草稿箱" };
const requestId = () => crypto.randomUUID();
const pageSize = 50;

export function MailSettings() {
  const [status, setStatus] = useState<MailStatus | null>(null);
  const [items, setItems] = useState<MailItem[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [detail, setDetail] = useState<MailDetail | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [reminderRoleId, setReminderRoleId] = useState("");
  const [host, setHost] = useState("imap.qq.com");
  const [port, setPort] = useState(993);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [page, setPage] = useState(0);
  const [processOnSync, setProcessOnSync] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const recoveringJob = useRef(false);

  const refresh = async () => {
    const [mail, list, roleData, reminder] = await Promise.all([
      api<MailStatus>("/api/mail/status"),
      api<{ items: MailItem[] }>(`/api/mail/items?limit=${pageSize}&offset=${page * pageSize}`),
      api<{ roles: Role[] }>("/api/roles"),
      api<{ roleId: string | null }>("/api/mail/reminder-role"),
    ]);
    setStatus(mail); setItems(list.items); setRoles(roleData.roles); setReminderRoleId(reminder.roleId ?? "");
    if (mail.username) setUsername((value) => value || mail.username);
    if (mail.candidate?.username) { setUsername((value) => value || mail.candidate!.username); setHost(mail.candidate.host); setPort(mail.candidate.port); }
    if (selectedId && list.items.some((item) => item.id === selectedId)) {
      setDetail(await api<MailDetail>(`/api/mail/items/${encodeURIComponent(selectedId)}`));
    }
  };

  useEffect(() => {
    void refresh().catch((cause) => setError(errorMessage(cause, "无法读取邮件状态"))).finally(() => setLoading(false));
  }, [page]);

  const run = async (action: () => Promise<unknown>, success: (result: unknown) => string = () => "操作已完成") => {
    setBusy(true); setError(""); setMessage("");
    try { const result = await action(); setMessage(success(result)); await refresh(); }
    catch (cause) { setError(errorMessage(cause, "邮件操作失败")); }
    finally { setBusy(false); }
  };

  const pollJob = async (jobId: string) => {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      const response = await api<{ job: MailJob }>(`/api/mail/jobs/${encodeURIComponent(jobId)}`);
      const job = response.job;
      if (job.status === "queued") setMessage("任务排队中…");
      if (job.status === "running") setMessage("任务处理中…");
      if (job.status === "failed") throw new Error(job.error || "邮件任务失败");
      if (job.status === "completed") return job.result;
    }
  };

  const runJob = async (start: () => Promise<{ job: { jobId: string } }>, success: (result: unknown) => string) => run(async () => {
    const accepted = await start();
    localStorage.setItem("meido.mail.active-job", accepted.job.jobId);
    setMessage("任务已受理，等待后台处理…");
    try { return await pollJob(accepted.job.jobId); }
    finally { localStorage.removeItem("meido.mail.active-job"); }
  }, success);

  useEffect(() => {
    const jobId = localStorage.getItem("meido.mail.active-job");
    if (!jobId || recoveringJob.current) return;
    recoveringJob.current = true;
    void run(() => pollJob(jobId), () => "邮件任务已完成").finally(() => {
      localStorage.removeItem("meido.mail.active-job");
      recoveringJob.current = false;
    });
  }, []);

  const select = async (id: string) => {
    setSelectedId(id); setError("");
    try { setDetail(await api<MailDetail>(`/api/mail/items/${encodeURIComponent(id)}`)); }
    catch (cause) { setError(errorMessage(cause, "无法读取邮件详情")); }
  };

  const saveCandidate = () => run(async () => {
    await api("/api/mail/connection", { method: "PUT", body: JSON.stringify({ host, port, username, password, useSsl: true }) });
    setPassword("");
    return null;
  }, () => "连接信息已保存在系统凭据库，请继续测试");

  const test = (writeTest: boolean) => run(
    () => api("/api/mail/connection/test", { method: "POST", body: JSON.stringify({ writeTest }) }),
    () => writeTest ? "测试草稿已写入 QQ 草稿箱" : "收件箱、已发送和草稿箱均可访问",
  );

  const task = (kind: "process" | "retry" | "regenerate") => {
    if (!selectedId) return;
    const path = kind === "regenerate" ? "regenerate" : kind;
    return runJob(
      () => api(`/api/mail/items/${encodeURIComponent(selectedId)}/${path}`, { method: "POST", body: JSON.stringify({ requestId: requestId() }) }),
      (result) => {
        const value = result as { status?: string; draftId?: string | null };
        return kind === "regenerate" ? "已创建新的 QQ 草稿，原草稿会保留" : value.status === "drafted" ? "回复草稿已写入 QQ 草稿箱" : value.status === "no_reply" ? "该邮件无需回复" : "邮件处理完成";
      },
    );
  };

  if (loading) return <Placeholder loading>正在加载邮件设置…</Placeholder>;
  const maxPage = Math.max(0, Math.ceil((status?.items ?? 0) / pageSize) - 1);

  return <section className="mail-settings" aria-labelledby="mail-settings-title">
    <header className="mail-header">
      <div className="owner-settings-title"><span className="owner-settings-icon"><Icon name="todo" size={20} /></span><div><h2 id="mail-settings-title">QQ 邮件</h2><p className="muted">自动整理回复草稿 · 发送由你在 QQ 邮箱完成</p></div></div>
      <div className="owner-plugin-control"><span className={`owner-plugin-state${status?.enabled ? " is-enabled" : ""}`}><span aria-hidden="true" />{status?.enabled ? "运行中" : status?.verified ? "已暂停" : "未验证"}</span><label className="plugin-switch" title="启用或暂停邮件自动处理"><input type="checkbox" checked={Boolean(status?.enabled)} disabled={busy || !status?.autoDraftReady} aria-label="启用邮件服务" onChange={(event) => void run(() => api(`/api/mail/${event.target.checked ? "enable" : "pause"}`, { method: "POST" }), () => event.target.checked ? "邮件服务已启用" : "邮件服务已暂停")} /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
    </header>
    {error && <Notice>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}{status?.lastError && <Notice>{status.lastError}</Notice>}

    <div className="mail-status-strip"><div><span>账号</span><strong>{status?.username || "未配置"}</strong></div><div><span>同步邮件</span><strong>{status?.items ?? 0}</strong></div><div><span>关联草稿</span><strong>{status?.drafts ?? 0}</strong></div><div><span>最近同步</span><strong>{status?.lastSyncAt ? new Date(status.lastSyncAt).toLocaleString() : "尚无记录"}</strong></div></div>

    <section className="mail-section" aria-labelledby="mail-connection-title"><div className="mail-section-heading"><div><h3 id="mail-connection-title">邮箱连接</h3><p className="muted">QQ 邮箱授权码 · SSL IMAP 993</p></div>{status?.verified && <span className="owner-connection-badge is-connected"><span aria-hidden="true" />已验证</span>}</div>
      <div className="mail-connection-grid"><div className="field"><label htmlFor="mail-host">IMAP 地址</label><input id="mail-host" value={host} disabled={busy} onChange={(event) => setHost(event.target.value)} /></div><div className="field"><label htmlFor="mail-port">端口</label><input id="mail-port" type="number" min={1} max={65535} value={port} disabled={busy} onChange={(event) => setPort(Number(event.target.value))} /></div><div className="field"><label htmlFor="mail-username">QQ 邮箱</label><input id="mail-username" type="email" autoComplete="username" value={username} disabled={busy} onChange={(event) => setUsername(event.target.value)} /></div><div className="field"><label htmlFor="mail-password">邮箱授权码</label><input id="mail-password" type="password" autoComplete="new-password" value={password} placeholder="仅本次填写，不会回显" disabled={busy} onChange={(event) => setPassword(event.target.value)} /></div></div>
      <div className="mail-action-row"><button type="button" disabled={busy || !username.trim() || !password} onClick={() => void saveCandidate()}>保存待测连接</button><button type="button" disabled={busy || !status?.candidate} onClick={() => void test(false)}>测试文件夹访问</button><button type="button" className="primary" disabled={busy || !status?.candidate?.connection_tested} onClick={() => void test(true)}>写入测试草稿</button></div>
      {status?.candidate?.test_draft_present && !status.candidate.test_sent && <div className="mail-test-confirm"><p>请在 QQ 邮箱网页版找到测试草稿，修改内容并手动发送给自己。Meido 只会核对已发送标记，不会替你发送。</p><button type="button" disabled={busy} onClick={() => void run(() => api("/api/mail/connection/confirm-test-send", { method: "POST" }), () => "已确认 QQ 网页端编辑和发送能力；连接已保存")}>我已在 QQ 网页端修改并发送测试草稿</button></div>}
      {status?.autoDraftReady && <p className="mail-verified-line">已验证 QQ 网页端可编辑和手动发送草稿。Meido 不会发送、删除、归档或移动邮件。</p>}
    </section>

    <section className="mail-section" aria-labelledby="mail-operations-title">
      <div className="mail-section-heading"><div><h3 id="mail-operations-title">邮件处理</h3><p className="muted">自动检查每分钟运行；暂停后仍可手动同步和查看</p></div><div className="mail-action-row"><button type="button" disabled={busy || !status?.verified} onClick={() => void runJob(() => api("/api/mail/sync", { method: "POST", body: JSON.stringify({ process: processOnSync, requestId: requestId() }) }), (result) => { const value = result as { discovered?: number; processed?: number; errors?: string[] }; return `同步完成：发现 ${value.discovered ?? 0} 封，处理 ${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>立即同步</button><button type="button" className="primary" disabled={busy || !status?.autoDraftReady} onClick={() => void runJob(() => api("/api/mail/process-pending", { method: "POST", body: JSON.stringify({ includeUncertain: true, requestId: requestId() }) }), (result) => { const value = result as { processed?: number; errors?: string[] }; return `处理完成：${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>处理全部待处理邮件</button></div></div>
      <label className="mail-process-option"><input type="checkbox" checked={processOnSync} disabled={busy} onChange={(event) => setProcessOnSync(event.target.checked)} />同步后处理新邮件</label>
      {status?.lastErrors?.length ? <ul className="mail-errors">{status.lastErrors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : null}
      <div className="mail-workspace"><div><div className="mail-list" aria-label="已同步邮件">{items.length ? items.map((item) => <button key={item.id} type="button" className={`mail-list-item${selectedId === item.id ? " is-selected" : ""}`} onClick={() => void select(item.id)}><span className="mail-item-title">{item.subject || "（无主题）"}</span><span className="mail-item-sender">{item.sender}</span><span className="mail-item-meta"><span>{labels[item.status] ?? item.status}{item.draft?.stale ? " · 可能过时" : ""}</span><time>{item.receivedAt ? new Date(item.receivedAt).toLocaleDateString() : ""}</time></span>{item.lastError && <span className="mail-item-error">{item.lastError}</span>}</button>) : <p className="mail-empty">还没有同步邮件</p>}</div><div className="mail-pagination"><button type="button" disabled={busy || page === 0} aria-label="上一页邮件" onClick={() => setPage((value) => Math.max(0, value - 1))}>←</button><span>{page + 1} / {maxPage + 1}</span><button type="button" disabled={busy || page >= maxPage} aria-label="下一页邮件" onClick={() => setPage((value) => Math.min(maxPage, value + 1))}>→</button></div></div>
        <div className="mail-detail">{detail ? <><div className="mail-detail-heading"><div><span className="muted">{detail.item.sender} · {new Date(detail.item.receivedAt).toLocaleString()}</span><h4>{detail.item.subject || "（无主题）"}</h4><span className="muted">回复至 {detail.item.replyTo}</span></div><a href="https://mail.qq.com/" target="_blank" rel="noreferrer" aria-label="在 QQ 邮箱中审核草稿">打开 QQ 邮箱 ↗</a></div>
          <div className="mail-thread" aria-label="邮件线程">{detail.thread.map((entry) => <article key={entry.id} className={`mail-thread-message${entry.id === detail.item.id ? " is-current" : ""}`}><header><strong>{entry.sender}</strong><time>{entry.receivedAt ? new Date(entry.receivedAt).toLocaleString() : ""}</time></header><h5>{entry.subject || "（无主题）"}</h5><div className="mail-body">{entry.body || "（邮件没有正文）"}</div></article>)}</div>
          {detail.drafts.map((draft) => <section key={draft.id} className="mail-draft"><div><strong>{labels[draft.status] ?? draft.status}</strong><span>{draft.recipients}</span>{draft.stale && <span>可能已过时</span>}{draft.remoteMissing && <span>QQ 草稿箱中已不存在</span>}</div><p>{draft.body}</p><small>{new Date(draft.updatedAt).toLocaleString()}</small></section>)}
          <div className="mail-action-row"><button type="button" disabled={busy || !status?.autoDraftReady || ["drafted", "no_reply"].includes(detail.item.status)} onClick={() => void task("process")}>处理邮件</button>{["failed", "uncertain"].includes(detail.item.status) && <button type="button" disabled={busy} onClick={() => void task("retry")}>安全重试</button>}{detail.drafts.length > 0 && <button type="button" disabled={busy || !status?.autoDraftReady} onClick={() => void task("regenerate")}>创建新草稿版本</button>}<button type="button" disabled={busy || !status?.autoDraftReady || !detail.thread.some((entry) => ["new", "failed", "uncertain"].includes(entry.status))} onClick={() => void runJob(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/process`, { method: "POST", body: JSON.stringify({ requestId: requestId() }) }), (result) => { const value = result as { processed?: number; errors?: string[] }; return `线程处理完成：${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>处理整个线程</button></div>{detail.item.lastError && <p className="mail-item-error">{detail.item.lastError}</p>}</> : <p className="mail-empty">选择一封邮件查看正文和草稿状态</p>}</div>
      </div>
    </section>

    <section className="mail-section" aria-labelledby="mail-reminder-title"><div className="mail-section-heading"><div><h3 id="mail-reminder-title">草稿提醒角色</h3><p className="muted">每批新建草稿汇总为一条会话主动消息，并报告处理失败数</p></div><select aria-label="邮件提醒角色" value={reminderRoleId} disabled={busy} onChange={(event) => { const value = event.target.value; setReminderRoleId(value); void run(() => api("/api/mail/reminder-role", { method: "PUT", body: JSON.stringify({ roleId: value || null }) }), () => "邮件提醒角色已更新"); }}><option value="">不发送提醒</option>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></div></section>
  </section>;
}
