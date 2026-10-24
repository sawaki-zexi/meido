import { useEffect, useRef, useState } from "react";
import { ApiError, api, errorMessage } from "../../api/client";
import { Icon } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { MailPreparation, type Preparation } from "./MailPreparation";

type MailStatus = {
  host: string; port: number; username: string; useSsl: boolean; enabled: boolean; paused: boolean;
  verified: boolean; autoDraftReady: boolean; folders: { inbox: string; sent: string; drafts: string };
  lastError: string | null; lastSyncAt: string | null; items: number; drafts: number;
  syncStates?: { folder: string; lastUid: number; uidValidity: string; updatedAt?: string | null }[];
  queue?: { jobs: Record<string, number>; tasks: Record<string, number> };
  reconciliation?: { total: number; due: number; maxAttempts: number; oldestUpdatedAt?: string | null };
  followup?: { enabled: boolean; delayHours: number; autoDraft?: boolean };
  candidate?: { host: string; port: number; username: string; connection_tested: boolean; test_draft_present: boolean; test_sent: boolean } | null;
  testDraft?: { status: string; created_at: string; sent_at?: string; error?: string } | null;
  lastErrors: string[];
};
type SentAssociation = { state: string; sentUid?: string | null; sentMessageId?: string | null; sentAt?: string | null; matchBasis?: string; candidateCount?: number; checkedAt?: string | null; lastError?: string | null } | null;
type FollowupEvidence = { eventId: string; itemId?: string; owner?: string; kind?: string; status?: string; summary?: string; at?: string | null; dueAt?: string | null; fulfilledByEventId?: string | null };
type FollowupAnalysis = { classifier?: string; nextAction?: string; nextDueAt?: string | null; openQuestions?: FollowupEvidence[]; commitments?: FollowupEvidence[]; evidenceEventIds?: string[] };
type ThreadAction = {
  threadId: string; latestMailId: string; state: string; reason: string; nextAction: string;
  nextDueAt?: string | null; latestEventAt?: string | null; nextReminderAt?: string | null;
  reminderEnabled: boolean; lastError?: string | null; subject: string; sender: string; recipients: string;
  latestDirection: string; latestStatus: string; itemCount: number; draftCount: number; unconfirmedCount: number;
  openQuestionCount: number; commitmentCount: number; needsInput?: boolean; overdue?: boolean; needsVerification?: boolean; analysis?: FollowupAnalysis;
};
type MailItem = { id: string; threadId: string; sender: string; subject: string; receivedAt: string; status: string; needsReply: boolean | null; draftId: string | null; lastError: string | null; unconfirmedAppend?: boolean; draft?: { id: string; status: string; stale: boolean; remoteMissing: boolean; recipients: string; sentAssociation?: SentAssociation } };
type MailDetail = {
  item: { id: string; threadId: string; sender: string; replyTo: string; subject: string; receivedAt: string; body: string; status: string; lastError: string | null; direction?: string };
  thread: { id: string; sender: string; subject: string; receivedAt: string; body: string; status: string; direction?: string }[];
  drafts: { id: string; revision: number; status: string; stale: boolean; remoteMissing: boolean; body: string; subject: string; recipients: string; updatedAt: string; lastError?: string | null; sentAssociation?: SentAssociation; preparation?: Preparation | null }[];
  preparation?: Preparation | null;
  followup?: { threadId: string; state: string; reason: string; latestEventAt?: string | null; nextReminderAt?: string | null; reminderEnabled: boolean; lastError?: string | null; analysis?: FollowupAnalysis } | null;
  unconfirmedAppend?: boolean;
  unconfirmedAppendDetails?: { lastCheckedAt?: string | null; nextCheckAt?: string | null; reconcileAttempts?: number; due?: boolean } | null;
  threadDossier?: { mailIds: string[]; latestMailId: string; drafts: { id: string; mailId: string; revision: number; status: string; stale: boolean; remoteMissing: boolean; updatedAt: string; recipients: string; sentAssociation?: SentAssociation }[]; preparations: Preparation[]; unconfirmedAppends: Record<string, unknown>[] };
};
type Role = { id: string; name: string };
type MailJob = { jobId: string; status: "queued" | "running" | "completed" | "failed"; error: string | null; result: unknown };
type ReminderBatch = { batchId: string; status: "pending" | "sending" | "failed" | "delivered" | "completed"; failureCount: number; failureSummaries: string[]; lastError: string | null; createdAt: string; deliveredAt: string | null };
type MailStats = { total: number; byStatus: Record<string, number>; drafts: number; unconfirmedAppends: number; followups: Record<string, number>; queue?: { jobs: Record<string, number>; tasks: Record<string, number> }; reconciliation?: { total: number; due: number; maxAttempts: number; oldestUpdatedAt?: string | null } };

const labels: Record<string, string> = { new: "待处理", no_reply: "无需回复", needs_input: "待你补充", sent: "已发送", queued: "排队中", processing: "处理中", drafted: "草稿已创建", failed: "失败", uncertain: "待核实", created: "QQ 草稿", manual_modified: "主人已修改", stale: "可能过时", missing: "已不在草稿箱", pending: "发送待确认", matched: "已找到发送副本", ambiguous: "发送待核实", unknown: "发送状态未知", sent_pending: "发送待确认", sent_matched: "已找到发送副本", sent_ambiguous: "发送待核实", sent_unknown: "发送状态未知" };
const statusOptions = ["", "new", "needs_input", "no_reply", "drafted", "sent", "processing", "failed", "uncertain", "sent_pending", "sent_matched", "sent_ambiguous", "sent_unknown"] as const;
const requestId = () => crypto.randomUUID();
const pageSize = 50;
const jobPollTimeoutMs = 5 * 60 * 1000;
const jobResultMessage = (result: unknown) => {
  const value = result as { status?: string } | null;
  if (value?.status === "uncertain") return "草稿结果仍待核实；系统没有重复追加，请核对 QQ 草稿箱。";
  if (value?.status === "drafted") return "回复草稿已写入 QQ 草稿箱。";
  if (value?.status === "needs_input") return "需要你补充信息后继续。";
  if (value?.status === "no_reply") return "该邮件无需回复。";
  return "邮件任务已完成";
};
const sentAssociationLabel = (association?: SentAssociation) => association ? labels[association.state] ?? association.state : "";
const followupActionLabels: Record<string, string> = {
  none: "无需继续跟进",
  review_or_reply: "查看并决定是否回复",
  owner_complete_commitment: "主人完成自己的承诺",
  wait_counterparty: "等待对方回复",
  wait_counterparty_commitment: "等待对方兑现承诺",
  verify_thread: "核实线程信息",
  needs_input: "待补充",
  overdue: "已到期",
  needs_verification: "需核实",
};
const followupOwnerLabels: Record<string, string> = { owner: "主人", counterparty: "对方" };
const followupKindLabels: Record<string, string> = { question: "问题", inbound_question: "待处理问题", owner_commitment: "主人承诺", counterparty_commitment: "对方承诺" };
const followupStateLabels: Record<string, string> = { to_reply: "待主人处理", awaiting_reply: "等待对方", closed: "已关闭", unknown: "需核实" };

function FollowupAnalysisPanel({ followup, thread, onSelect }: { followup: NonNullable<MailDetail["followup"]>; thread: MailDetail["thread"]; onSelect: (id: string) => void }) {
  const analysis = followup.analysis;
  if (!analysis) return null;
  const evidence = new Map(thread.map((entry) => [entry.id, entry]));
  const detailedEvidenceIds = new Set([...(analysis.openQuestions ?? []), ...(analysis.commitments ?? [])].map((entry) => entry.eventId));
  const summaryEvidenceIds = (analysis.evidenceEventIds ?? []).filter((id) => !detailedEvidenceIds.has(id));
  const renderEvidence = (entry: FollowupEvidence, index: number) => {
    const source = evidence.get(entry.eventId);
    const title = source?.subject || entry.summary || entry.eventId;
    return <li key={`${entry.eventId}-${index}`}><strong>{followupKindLabels[entry.kind ?? ""] ?? "事项"}</strong><span>{followupOwnerLabels[entry.owner ?? ""] ?? entry.owner ?? ""}</span>{source ? <button type="button" className="mail-evidence-link" aria-label={`查看依据邮件：${title}`} onClick={() => onSelect(source.id)}>{title}</button> : <span>{title}</span>}{entry.status && entry.status !== "open" && <small>{entry.status === "fulfilled" ? "已兑现" : entry.status}</small>}{entry.dueAt && <small>截止 {new Date(entry.dueAt).toLocaleString()}</small>}</li>;
  };
  return <div className="mail-followup-analysis" aria-label="线程判断依据">
    <div><strong>下一步：{followupActionLabels[analysis.nextAction ?? ""] ?? analysis.nextAction ?? "需要核实"}</strong><span className="muted">{followup.reason}</span>{analysis.nextDueAt && <small>最近截止：{new Date(analysis.nextDueAt).toLocaleString()}</small>}</div>
    {(analysis.openQuestions?.length ?? 0) > 0 && <div><span className="mail-followup-label">未解决问题</span><ul>{analysis.openQuestions?.map(renderEvidence)}</ul></div>}
    {(analysis.commitments?.length ?? 0) > 0 && <div><span className="mail-followup-label">承诺</span><ul>{analysis.commitments?.map(renderEvidence)}</ul></div>}
    {summaryEvidenceIds.length > 0 && <small>依据邮件：{summaryEvidenceIds.map((id, index) => { const source = evidence.get(id); const title = source?.subject || id; return source ? <span key={id}>{index > 0 ? "、" : ""}<button type="button" className="mail-evidence-link" aria-label={`查看依据邮件：${title}`} onClick={() => onSelect(source.id)}>{title}</button></span> : <span key={id}>{index > 0 ? "、" : ""}{title}</span>; })}</small>}
  </div>;
}

export function MailSettings() {
  const [status, setStatus] = useState<MailStatus | null>(null);
  const [items, setItems] = useState<MailItem[]>([]);
  const [visibleCount, setVisibleCount] = useState(0);
  const [roles, setRoles] = useState<Role[]>([]);
  const [reminderBatches, setReminderBatches] = useState<ReminderBatch[]>([]);
  const [stats, setStats] = useState<MailStats | null>(null);
  const [threadActions, setThreadActions] = useState<ThreadAction[]>([]);
  const [threadActionCount, setThreadActionCount] = useState(0);
  const [viewMode, setViewMode] = useState<"messages" | "actions">("messages");
  const [actionFilter, setActionFilter] = useState("");
  const [threadStateFilter, setThreadStateFilter] = useState("");
  const [detail, setDetail] = useState<MailDetail | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [showAllThreadMessages, setShowAllThreadMessages] = useState(false);
  const [reminderRoleId, setReminderRoleId] = useState("");
  const [host, setHost] = useState("imap.qq.com");
  const [port, setPort] = useState(993);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [page, setPage] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [searchText, setSearchText] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [processOnSync, setProcessOnSync] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const recoveringJob = useRef(false);
  const preferencesLoaded = useRef(false);
  const followupLoaded = useRef(false);
  const loadedAccount = useRef("");
  const [writingStyle, setWritingStyle] = useState("");
  const [roleAccess, setRoleAccess] = useState(false);
  const [followupEnabled, setFollowupEnabled] = useState(false);
  const [followupDelayHours, setFollowupDelayHours] = useState(24);
  const [followupAutoDraft, setFollowupAutoDraft] = useState(false);
  const [answer, setAnswer] = useState("");
  const [confirmUncertainVersion, setConfirmUncertainVersion] = useState(false);
  const answerAttempt = useRef<{ questionId: string; answer: string; requestId: string } | null>(null);

  const refresh = async () => {
    const [mail, list, roleData, reminder, batches, summary, preferences] = await Promise.all([
      api<MailStatus>("/api/mail/status"),
      api<{ items: MailItem[]; count?: number }>(`/api/mail/items?limit=${pageSize}&offset=${page * pageSize}${statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : ""}${searchQuery ? `&query=${encodeURIComponent(searchQuery)}` : ""}`),
      api<{ roles: Role[] }>("/api/roles"),
      api<{ roleId: string | null }>("/api/mail/reminder-role"),
      api<{ batches: ReminderBatch[] }>("/api/mail/reminder-batches?limit=8"),
      api<MailStats>(`/api/mail/stats${searchQuery ? `?query=${encodeURIComponent(searchQuery)}` : ""}`),
      api<{ writingStyle: string; roleAccess: boolean }>("/api/mail/preferences"),
    ]);
    const actions = viewMode === "actions"
      ? await api<{ threads: ThreadAction[]; count: number }>(`/api/mail/threads?limit=${pageSize}&offset=${page * pageSize}${threadStateFilter ? `&state=${encodeURIComponent(threadStateFilter)}` : ""}${actionFilter ? `&action=${encodeURIComponent(actionFilter)}` : ""}${searchQuery ? `&query=${encodeURIComponent(searchQuery)}` : ""}`)
      : null;
    const accountChanged = loadedAccount.current !== "" && loadedAccount.current !== mail.username;
    loadedAccount.current = mail.username;
    setStatus(mail); setItems(list.items); setVisibleCount(list.count ?? mail.items); setRoles(roleData.roles); setReminderRoleId(reminder.roleId ?? ""); setReminderBatches(batches.batches); setStats(summary);
    if (actions) { setThreadActions(actions.threads); setThreadActionCount(actions.count); }
    if (accountChanged) { setSelectedId(""); setDetail(null); setAnswer(""); setConfirmUncertainVersion(false); answerAttempt.current = null; }
    if (!preferencesLoaded.current) { setWritingStyle(preferences.writingStyle); setRoleAccess(preferences.roleAccess); preferencesLoaded.current = true; }
    if (!followupLoaded.current && mail.followup) { setFollowupEnabled(mail.followup.enabled); setFollowupDelayHours(mail.followup.delayHours); setFollowupAutoDraft(Boolean(mail.followup.autoDraft)); followupLoaded.current = true; }
    if (mail.username) setUsername((value) => value || mail.username);
    if (mail.candidate?.username) { setUsername((value) => value || mail.candidate!.username); setHost(mail.candidate.host); setPort(mail.candidate.port); }
    const selectedVisible = viewMode === "actions"
      ? Boolean(actions?.threads.some((thread) => thread.latestMailId === selectedId))
      : list.items.some((item) => item.id === selectedId);
    if (!accountChanged && selectedId && selectedVisible) {
      setDetail(await api<MailDetail>(`/api/mail/items/${encodeURIComponent(selectedId)}`));
    }
  };

  useEffect(() => {
    void refresh().catch((cause) => setError(errorMessage(cause, "无法读取邮件状态"))).finally(() => setLoading(false));
  }, [page, statusFilter, searchQuery, viewMode, actionFilter, threadStateFilter]);

  const run = async (action: () => Promise<unknown>, success: (result: unknown) => string = () => "操作已完成") => {
    setBusy(true); setError(""); setMessage("");
    try { const result = await action(); setMessage(success(result)); await refresh(); }
    catch (cause) { setError(errorMessage(cause, "邮件操作失败")); }
    finally { setBusy(false); }
  };

  const pollJob = async (jobId: string) => {
    const startedAt = Date.now();
    let delay = 700;
    while (Date.now() - startedAt < jobPollTimeoutMs) {
      await new Promise((resolve) => window.setTimeout(resolve, delay));
      let response: { job: MailJob };
      try {
        response = await api<{ job: MailJob }>(`/api/mail/jobs/${encodeURIComponent(jobId)}`);
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 404) localStorage.removeItem("meido.mail.active-job");
        throw cause;
      }
      const job = response.job;
      if (job.status === "queued") setMessage("任务排队中…");
      if (job.status === "running") setMessage("任务处理中…");
      if (job.status === "failed") {
        localStorage.removeItem("meido.mail.active-job");
        throw new Error(job.error || "邮件任务失败");
      }
      if (job.status === "completed") {
        localStorage.removeItem("meido.mail.active-job");
        return job.result;
      }
      delay = job.status === "queued" ? Math.min(3000, delay + 300) : 700;
    }
    throw new Error("邮件任务等待时间过长；任务仍在后台运行，刷新页面可继续查询");
  };

  const runJob = async (start: () => Promise<{ job: { jobId: string } }>, success: (result: unknown) => string) => run(async () => {
    const accepted = await start();
    localStorage.setItem("meido.mail.active-job", accepted.job.jobId);
    setMessage("任务已受理，等待后台处理…");
    return pollJob(accepted.job.jobId);
  }, success);

  useEffect(() => {
    const jobId = localStorage.getItem("meido.mail.active-job");
    if (!jobId || recoveringJob.current) return;
    recoveringJob.current = true;
    void run(() => pollJob(jobId), jobResultMessage).finally(() => {
      recoveringJob.current = false;
    });
  }, []);

  const select = async (id: string) => {
    setSelectedId(id); setError(""); setAnswer(""); setConfirmUncertainVersion(false); setShowAllThreadMessages(false); answerAttempt.current = null;
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

  const task = (kind: "process" | "retry" | "regenerate", options: { confirmUncertainNewVersion?: boolean } = {}) => {
    if (!selectedId) return;
    const path = kind === "regenerate" ? "regenerate" : kind;
    return runJob(
      () => api(`/api/mail/items/${encodeURIComponent(selectedId)}/${path}`, { method: "POST", body: JSON.stringify({ requestId: requestId(), ...(options.confirmUncertainNewVersion ? { confirmUncertainNewVersion: true } : {}) }) }),
      (result) => {
        const value = result as { status?: string; draftId?: string | null };
        return value.status === "uncertain" ? "草稿结果仍待核实；系统没有重复追加，请核对 QQ 草稿箱。" : value.status === "drafted" ? (kind === "regenerate" ? "已创建新的 QQ 草稿，原草稿会保留" : kind === "retry" ? "已找到原 QQ 草稿，未重复追加" : "回复草稿已写入 QQ 草稿箱") : value.status === "no_reply" ? "该邮件无需回复" : value.status === "needs_input" ? "需要你补充信息后继续" : "邮件处理完成";
      },
    );
  };

  const submitAnswer = () => {
    const questionId = detail?.preparation?.id;
    if (!questionId || !answer.trim()) return;
    if (answerAttempt.current?.questionId !== questionId || answerAttempt.current.answer !== answer.trim()) {
      answerAttempt.current = { questionId, answer: answer.trim(), requestId: requestId() };
    }
    const payload = answerAttempt.current;
    return runJob(() => api(`/api/mail/items/${encodeURIComponent(selectedId)}/answer`, { method: "POST", body: JSON.stringify(payload) }),
      (result) => `补充已处理：${labels[(result as { status: string }).status] ?? "请查看结果"}`);
  };

  if (loading) return <Placeholder loading>正在加载邮件设置…</Placeholder>;
  const maxPage = Math.max(0, Math.ceil(visibleCount / pageSize) - 1);
  const maxActionPage = Math.max(0, Math.ceil(threadActionCount / pageSize) - 1);

  return <section className="mail-settings" aria-labelledby="mail-settings-title">
    <header className="mail-header">
      <div className="owner-settings-title"><span className="owner-settings-icon"><Icon name="mail" size={20} /></span><div><h2 id="mail-settings-title">QQ 邮件</h2><p className="muted">自动整理回复草稿 · 发送由你在 QQ 邮箱完成</p></div></div>
      <div className="owner-plugin-control"><span className={`owner-plugin-state${status?.enabled ? " is-enabled" : ""}`}><span aria-hidden="true" />{status?.enabled ? "运行中" : status?.verified ? "已暂停" : "未验证"}</span><label className="plugin-switch" title="启用或暂停邮件自动处理"><input type="checkbox" checked={Boolean(status?.enabled)} disabled={busy || !status?.autoDraftReady} aria-label="启用邮件服务" onChange={(event) => void run(() => api(`/api/mail/${event.target.checked ? "enable" : "pause"}`, { method: "POST" }), () => event.target.checked ? "邮件服务已启用" : "邮件服务已暂停")} /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
    </header>
    {error && <Notice>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}{status?.lastError && <Notice>{status.lastError}</Notice>}

      <div className="mail-status-strip"><div><span>账号</span><strong>{status?.username || "未配置"}</strong></div><div><span>同步邮件</span><strong>{status?.items ?? 0}</strong></div><div><span>关联草稿</span><strong>{status?.drafts ?? 0}</strong></div><div><span>最近同步</span><strong>{status?.lastSyncAt ? new Date(status.lastSyncAt).toLocaleString() : "尚无记录"}</strong></div></div>
      {status?.syncStates?.length ? <div className="mail-sync-health" aria-label="邮件同步状态">{status.syncStates.map((state) => <span key={state.folder}>{state.folder} UID {state.lastUid}{state.uidValidity ? ` · 命名空间 ${state.uidValidity}` : " · 未提供命名空间"}</span>)}</div> : null}

    <section className="mail-section" aria-labelledby="mail-connection-title"><div className="mail-section-heading"><div><h3 id="mail-connection-title">邮箱连接</h3><p className="muted">QQ 邮箱授权码 · SSL IMAP 993</p></div>{status?.verified && <span className="owner-connection-badge is-connected"><span aria-hidden="true" />已验证</span>}</div>
      <div className="mail-connection-grid"><div className="field"><label htmlFor="mail-host">IMAP 地址</label><input id="mail-host" value={host} disabled={busy} onChange={(event) => setHost(event.target.value)} /></div><div className="field"><label htmlFor="mail-port">端口</label><input id="mail-port" type="number" min={1} max={65535} value={port} disabled={busy} onChange={(event) => setPort(Number(event.target.value))} /></div><div className="field"><label htmlFor="mail-username">QQ 邮箱</label><input id="mail-username" type="email" autoComplete="username" value={username} disabled={busy} onChange={(event) => setUsername(event.target.value)} /></div><div className="field"><label htmlFor="mail-password">邮箱授权码</label><input id="mail-password" type="password" autoComplete="new-password" value={password} placeholder="仅本次填写，不会回显" disabled={busy} onChange={(event) => setPassword(event.target.value)} /></div></div>
      <div className="mail-action-row"><button type="button" disabled={busy || !username.trim() || !password} onClick={() => void saveCandidate()}>保存待测连接</button><button type="button" disabled={busy || !status?.candidate} onClick={() => void test(false)}>测试文件夹访问</button><button type="button" className="primary" disabled={busy || !status?.candidate?.connection_tested} onClick={() => void test(true)}>写入测试草稿</button></div>
      {status?.candidate?.test_draft_present && !status.candidate.test_sent && <div className="mail-test-confirm"><p>请在 QQ 邮箱网页版找到测试草稿，修改内容并手动发送给自己。Meido 只会核对已发送标记，不会替你发送。</p><button type="button" disabled={busy} onClick={() => void run(() => api("/api/mail/connection/confirm-test-send", { method: "POST" }), () => "已确认 QQ 网页端编辑和发送能力；连接已保存")}>我已在 QQ 网页端修改并发送测试草稿</button></div>}
      {status?.autoDraftReady && <p className="mail-verified-line">已验证 QQ 网页端可编辑和手动发送草稿。Meido 不会发送、删除、归档或移动邮件。</p>}
    </section>

    <section className="mail-section" aria-labelledby="mail-preferences-title">
      <h3 id="mail-preferences-title">回复偏好</h3>
      <div className="field"><label htmlFor="mail-writing-style">邮件写作偏好</label><textarea id="mail-writing-style" maxLength={2000} rows={3} value={writingStyle} disabled={busy} placeholder="例如：简短、直接，保留原有称呼，不做额外承诺" onChange={(event) => setWritingStyle(event.target.value)} /></div>
      <label className="mail-process-option"><input type="checkbox" checked={roleAccess} disabled={busy} onChange={(event) => setRoleAccess(event.target.checked)} />允许角色管理邮件</label>
      <p className="muted">允许角色在主人对话中读取邮件、准备草稿和提交补充答案。最终发送仍由你完成。</p>
      <button type="button" disabled={busy} onClick={() => void run(() => api("/api/mail/preferences", { method: "PUT", body: JSON.stringify({ writingStyle, roleAccess }) }), () => "邮件偏好已保存")}>保存邮件偏好</button>
    </section>

    <section className="mail-section" aria-labelledby="mail-operations-title">
      <div className="mail-section-heading"><div><h3 id="mail-operations-title">邮件处理</h3><p className="muted">自动检查每分钟运行；暂停后仍可手动同步和查看</p></div><div className="mail-action-row"><button type="button" disabled={busy || !status?.verified} onClick={() => void runJob(() => api("/api/mail/sync", { method: "POST", body: JSON.stringify({ process: processOnSync, requestId: requestId() }) }), (result) => { const value = result as { discovered?: number; processed?: number; errors?: string[] }; return `同步完成：发现 ${value.discovered ?? 0} 封，处理 ${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>立即同步</button><button type="button" className="primary" disabled={busy || !status?.autoDraftReady} onClick={() => void runJob(() => api("/api/mail/process-pending", { method: "POST", body: JSON.stringify({ includeUncertain: true, requestId: requestId() }) }), (result) => { const value = result as { processed?: number; errors?: string[] }; return `处理完成：${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>处理全部待处理邮件</button></div></div>
      <label className="mail-process-option"><input type="checkbox" checked={processOnSync} disabled={busy} onChange={(event) => setProcessOnSync(event.target.checked)} />同步后处理新邮件</label>
      {status?.lastErrors?.length ? <ul className="mail-errors">{status.lastErrors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : null}
       <div className="mail-view-tabs" role="tablist" aria-label="邮件视图"><button type="button" role="tab" aria-selected={viewMode === "messages"} onClick={() => { setViewMode("messages"); setPage(0); }}>邮件</button><button type="button" role="tab" aria-selected={viewMode === "actions"} onClick={() => { setViewMode("actions"); setPage(0); }}>线程行动</button></div>
       <div className="mail-workspace">
         <div>
           <div className={`mail-list-toolbar${viewMode === "actions" ? " is-actions" : ""}`}>
             <form className="mail-search-form" role="search" onSubmit={(event) => { event.preventDefault(); setSearchQuery(searchText.trim().slice(0, 200)); setPage(0); }}>
               <label htmlFor="mail-search">搜索邮件</label>
               <input id="mail-search" type="search" value={searchText} placeholder="主题、发件人或正文" maxLength={200} disabled={busy} onChange={(event) => setSearchText(event.target.value)} />
               <button type="submit" aria-label="搜索邮件" title="搜索邮件" disabled={busy}><Icon name="search" size={16} /></button>
               {searchQuery && <button type="button" aria-label="清除邮件搜索" title="清除搜索" disabled={busy} onClick={() => { setSearchText(""); setSearchQuery(""); setPage(0); }}><Icon name="close" size={16} /></button>}
             </form>
              {viewMode === "messages" ? <><label htmlFor="mail-status-filter">筛选状态</label><select id="mail-status-filter" value={statusFilter} disabled={busy} onChange={(event) => { setStatusFilter(event.target.value); setPage(0); }}><option value="">全部来信</option>{statusOptions.slice(1).map((value) => <option key={value} value={value}>{labels[value]}</option>)}</select><span className="muted">{visibleCount} 封</span></> : <div className="mail-thread-filters"><div className="mail-filter-field"><label htmlFor="mail-thread-state-filter">线程状态</label><select id="mail-thread-state-filter" value={threadStateFilter} disabled={busy} onChange={(event) => { setThreadStateFilter(event.target.value); setPage(0); }}><option value="">全部状态</option>{Object.entries(followupStateLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><div className="mail-filter-field"><label htmlFor="mail-action-filter">筛选下一步</label><select id="mail-action-filter" value={actionFilter} disabled={busy} onChange={(event) => { setActionFilter(event.target.value); setPage(0); }}><option value="">全部线程</option>{Object.entries(followupActionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><span className="muted">{threadActionCount} 个线程</span></div>}
           </div>
           {stats && <div className="mail-summary" aria-label="邮件统计"><span>待处理 {stats.byStatus.new ?? 0}</span><span>待补充 {stats.byStatus.needs_input ?? 0}</span><span>草稿 {stats.drafts}</span><span>未核实写入 {stats.unconfirmedAppends}</span><span>跟进 {((stats.followups.to_reply ?? 0) + (stats.followups.awaiting_reply ?? 0))}</span><span>任务排队 {(stats.queue?.jobs.queued ?? 0) + (stats.queue?.tasks.queued ?? 0)}</span><span>处理中 {(stats.queue?.jobs.running ?? 0) + (stats.queue?.tasks.running ?? 0)}</span></div>}
           {stats?.reconciliation && stats.reconciliation.total > 0 ? <p className="muted">待核实写入：{stats.reconciliation.total} 项，其中 {stats.reconciliation.due} 项可立即对账；最多已尝试 {stats.reconciliation.maxAttempts} 次。</p> : null}
            {viewMode === "messages" ? <div className="mail-list" aria-label="已同步邮件">
              {items.length ? items.map((item) => (
               <button key={item.id} type="button" className={`mail-list-item${selectedId === item.id ? " is-selected" : ""}`} onClick={() => void select(item.id)}>
                 <span className="mail-item-title">{item.subject || "（无主题）"}</span>
                 <span className="mail-item-sender">{item.sender}</span>
                 <span className="mail-item-meta"><span>{labels[item.status] ?? item.status}{item.draft?.sentAssociation && item.status !== "sent" ? ` · ${sentAssociationLabel(item.draft.sentAssociation)}` : ""}{item.draft?.stale ? " · 可能过时" : ""}{item.unconfirmedAppend ? " · 待核实" : ""}</span><time>{item.receivedAt ? new Date(item.receivedAt).toLocaleDateString() : ""}</time></span>
                 {item.lastError && <span className="mail-item-error">{item.lastError}</span>}
               </button>
              )) : <p className="mail-empty">还没有同步邮件</p>}
            </div> : <div className="mail-thread-actions" aria-label="线程行动列表">
              {threadActions.length ? threadActions.map((thread) => <button key={thread.threadId} type="button" className={`mail-thread-action${selectedId === thread.latestMailId ? " is-selected" : ""}`} onClick={() => void select(thread.latestMailId)}><span className="mail-thread-action-heading"><strong>{thread.subject || "（无主题）"}</strong><span>{followupStateLabels[thread.state] ?? thread.state}</span></span><span className="mail-thread-action-meta">下一步：{followupActionLabels[thread.nextAction] ?? thread.nextAction} · {thread.sender} · {thread.itemCount} 封</span><span className="mail-thread-action-meta">{thread.nextDueAt ? `截止 ${new Date(thread.nextDueAt).toLocaleString()} · ` : ""}问题 {thread.openQuestionCount} · 承诺 {thread.commitmentCount} · 草稿 {thread.draftCount}{thread.needsInput ? " · 待补充" : ""}{thread.overdue ? " · 已到期" : ""}{thread.needsVerification ? ` · 需核实${thread.unconfirmedCount ? ` ${thread.unconfirmedCount} 项` : ""}` : ""}</span>{thread.lastError && <span className="mail-item-error">{thread.lastError}</span>}</button>) : <p className="mail-empty">还没有可显示的线程行动</p>}
            </div>}
            <div className="mail-pagination"><button type="button" disabled={busy || page === 0} aria-label="上一页邮件" onClick={() => setPage((value) => Math.max(0, value - 1))}>←</button><span>{page + 1} / {(viewMode === "actions" ? maxActionPage : maxPage) + 1}</span><button type="button" disabled={busy || page >= (viewMode === "actions" ? maxActionPage : maxPage)} aria-label="下一页邮件" onClick={() => setPage((value) => Math.min(viewMode === "actions" ? maxActionPage : maxPage, value + 1))}>→</button></div>
         </div>
         <div className="mail-detail">{detail ? <><div className="mail-detail-heading"><div><span className="muted">{detail.item.direction === "outbound" ? "已发送" : "来信"} · {detail.item.sender} · {new Date(detail.item.receivedAt).toLocaleString()}</span><h4>{detail.item.subject || "（无主题）"}</h4><span className="muted">回复至 {detail.item.replyTo}</span></div><a href="https://mail.qq.com/" target="_blank" rel="noreferrer" aria-label="在 QQ 邮箱中审核草稿">打开 QQ 邮箱 ↗</a></div>
           {detail.followup && <FollowupAnalysisPanel followup={detail.followup} thread={detail.thread} onSelect={(id) => void select(id)} />}
          {detail.threadDossier && <section className="mail-thread-dossier" aria-label="线程处理记录"><strong>线程处理记录</strong><span>共 {detail.threadDossier.mailIds.length} 封邮件 · {detail.threadDossier.drafts.length} 个草稿版本 · {detail.threadDossier.preparations.length} 次判断</span>{detail.threadDossier.unconfirmedAppends.length > 0 && <span className="mail-item-error">有 {detail.threadDossier.unconfirmedAppends.length} 项 QQ 写入待核实</span>}{detail.threadDossier.preparations.length > 0 && <ul>{detail.threadDossier.preparations.map((preparation) => { const source = preparation.mailId ? detail.thread.find((entry) => entry.id === preparation.mailId) : undefined; const title = source?.subject || preparation.mailId; return <li key={preparation.id}>{preparation.mailId ? <button type="button" className="mail-evidence-link" aria-label={`查看判断依据邮件：${title}`} onClick={() => void select(preparation.mailId!)}>{labels[preparation.decision] ?? preparation.decision}</button> : <span>{labels[preparation.decision] ?? preparation.decision}</span>}<span>{preparation.reason}</span>{preparation.stale && <small>可能已过时</small>}</li>; })}</ul>}</section>}
          {detail.preparation && <>
            <MailPreparation value={detail.preparation} onSelect={(id) => void select(id)} />
            {detail.preparation.decision === "needs_input" && <section className="mail-question" aria-label="待补充问题">
              <strong>待你补充</strong><ul>{detail.preparation.questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
              <div className="field"><label htmlFor="mail-answer">补充答案</label><textarea id="mail-answer" rows={3} maxLength={4000} value={answer} disabled={busy || detail.preparation.stale} onChange={(event) => setAnswer(event.target.value)} /></div>
              <button type="button" className="primary" disabled={busy || !answer.trim() || detail.preparation.stale} onClick={() => void submitAnswer()}>补充并继续起草</button>
            </section>}
            {(detail.preparation.stale || detail.item.status === "no_reply") && <button type="button" disabled={busy || !status?.autoDraftReady} onClick={() => void task("regenerate")}>重新判断邮件</button>}
          </>}
          {detail.thread.length > 6 && <div className="mail-thread-toggle"><span className="muted">线程共 {detail.thread.length} 封邮件</span><button type="button" onClick={() => setShowAllThreadMessages((value) => !value)}>{showAllThreadMessages ? "收起较早邮件" : `展开其余 ${detail.thread.length - 6} 封`}</button></div>}
          <div className="mail-thread" aria-label="邮件线程">{(showAllThreadMessages || detail.thread.length <= 6 ? detail.thread : detail.thread.slice(-6)).map((entry) => <article key={entry.id} className={`mail-thread-message${entry.id === detail.item.id ? " is-current" : ""}`}><header><strong>{entry.sender}</strong><span>{entry.direction === "outbound" ? "已发送" : "来信"}</span><time>{entry.receivedAt ? new Date(entry.receivedAt).toLocaleString() : ""}</time></header><h5>{entry.subject || "（无主题）"}</h5><div className="mail-body">{entry.body || "（邮件没有正文）"}</div></article>)}</div>
          {detail.drafts.map((draft) => <section key={draft.id} className="mail-draft"><div><strong>版本 {draft.revision} · {labels[draft.status] ?? draft.status}</strong><span>{draft.recipients}</span>{draft.sentAssociation && <span>{sentAssociationLabel(draft.sentAssociation)}{draft.sentAssociation.sentAt ? ` · ${new Date(draft.sentAssociation.sentAt).toLocaleString()}` : ""}</span>}{draft.stale && <span>可能已过时</span>}{draft.remoteMissing && draft.sentAssociation?.state !== "matched" && <span>QQ 草稿箱中已不存在</span>}{draft.sentAssociation?.lastError && <span>{draft.sentAssociation.lastError}</span>}</div><p>{draft.body}</p><small>{new Date(draft.updatedAt).toLocaleString()}</small>{draft.preparation && draft.preparation.id !== detail.preparation?.id && <MailPreparation value={draft.preparation} onSelect={(id) => void select(id)} />}</section>)}
          {detail.item.direction !== "outbound" && <>
            {detail.unconfirmedAppend && <section className="mail-uncertain-action" aria-label="待核实的 QQ 草稿写入"><strong>QQ 草稿写入结果待核实</strong><p>远端写入可能已经发生。核对原标记只会读取 QQ 草稿箱，不会再次追加。</p>{detail.unconfirmedAppendDetails && <small>已核对 {detail.unconfirmedAppendDetails.reconcileAttempts ?? 0} 次{detail.unconfirmedAppendDetails.nextCheckAt && !detail.unconfirmedAppendDetails.due ? `；下次自动核对 ${new Date(detail.unconfirmedAppendDetails.nextCheckAt).toLocaleString()}` : ""}</small>}<div className="mail-action-row"><button type="button" disabled={busy || !status?.autoDraftReady} onClick={() => void task("retry")}>核对 QQ 草稿标记</button>{!confirmUncertainVersion && <button type="button" disabled={busy} onClick={() => setConfirmUncertainVersion(true)}>我已检查 QQ 草稿箱，仍创建新版本</button>}{confirmUncertainVersion && <button type="button" className="primary" disabled={busy || !status?.autoDraftReady} onClick={() => void task("regenerate", { confirmUncertainNewVersion: true })}>确认创建独立草稿版本</button>}</div></section>}
            <div className="mail-action-row"><button type="button" disabled={busy || !status?.autoDraftReady || ["drafted", "no_reply"].includes(detail.item.status)} onClick={() => void task("process")}>处理邮件</button>{!["drafted", "no_reply"].includes(detail.item.status) && !detail.unconfirmedAppend && ["failed", "uncertain"].includes(detail.item.status) && <button type="button" disabled={busy} onClick={() => void task("retry")}>安全重试</button>}{detail.drafts.length > 0 && !detail.unconfirmedAppend && <button type="button" disabled={busy || !status?.autoDraftReady} onClick={() => void task("regenerate")}>创建新草稿版本</button>}<button type="button" disabled={busy || !status?.autoDraftReady || !detail.thread.some((entry) => ["new", "failed", "uncertain"].includes(entry.status))} onClick={() => void runJob(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/process`, { method: "POST", body: JSON.stringify({ requestId: requestId() }) }), (result) => { const value = result as { processed?: number; errors?: string[] }; return `线程处理完成：${value.processed ?? 0} 封，失败 ${value.errors?.length ?? 0} 封`; })}>处理整个线程</button></div>
          </>}{detail.item.lastError && <p className="mail-item-error">{detail.item.lastError}</p>}</> : <p className="mail-empty">选择一封邮件查看正文和草稿状态</p>}</div>
      </div>
    </section>

    <section className="mail-section" aria-labelledby="mail-followup-title">
      <div className="mail-section-heading"><div><h3 id="mail-followup-title">线程跟进提醒</h3><p className="muted">可选自动生成待审核催复草稿，但不会自动发送邮件</p></div><label className="plugin-switch" title="启用线程跟进提醒"><input type="checkbox" aria-label="启用线程跟进提醒" checked={followupEnabled} disabled={busy} onChange={(event) => setFollowupEnabled(event.target.checked)} /><span className="plugin-switch-track" aria-hidden="true"><span /></span></label></div>
      <div className="mail-connection-grid"><div className="field"><label htmlFor="mail-followup-delay">提醒间隔（小时）</label><input id="mail-followup-delay" type="number" min={1} max={720} value={followupDelayHours} disabled={busy} onChange={(event) => setFollowupDelayHours(Number(event.target.value))} /></div></div>
      <label className="mail-followup-auto-draft"><input type="checkbox" aria-label="到期自动生成催复草稿" checked={followupAutoDraft} disabled={busy} onChange={(event) => setFollowupAutoDraft(event.target.checked)} /><span>到期自动生成催复草稿</span></label>
      <p className="muted">仅在确认等待对方回复且线程没有新事件时生成；草稿仍需你在 QQ 邮箱审核和发送。</p>
      <button type="button" disabled={busy} onClick={() => void run(() => api("/api/mail/followup", { method: "PUT", body: JSON.stringify({ enabled: followupEnabled, delayHours: followupDelayHours, autoDraft: followupAutoDraft }) }), () => "线程跟进设置已保存")}>保存跟进设置</button>
      {detail?.followup && <div className="mail-followup-detail"><p className="muted">当前线程：{labels[detail.followup.state] ?? detail.followup.state}{detail.followup.nextReminderAt ? ` · 下次提醒 ${new Date(detail.followup.nextReminderAt).toLocaleString()}` : ""}</p><div className="mail-action-row">{detail.followup.state !== "to_reply" && <button type="button" disabled={busy} onClick={() => void run(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/followup`, { method: "PUT", body: JSON.stringify({ state: "to_reply" }) }), () => "已标记为待主人处理")}>标记待主人处理</button>}{detail.followup.state !== "awaiting_reply" && <button type="button" disabled={busy} onClick={() => void run(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/followup`, { method: "PUT", body: JSON.stringify({ state: "awaiting_reply" }) }), () => "已标记为等待对方回复")}>标记等待对方回复</button>}<button type="button" disabled={busy} onClick={() => void run(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/followup`, { method: "PUT", body: JSON.stringify({ state: detail.followup?.state === "closed" ? "clear" : "closed" }) }), () => detail.followup?.state === "closed" ? "已恢复自动判断" : "本线程跟进已关闭")}>{detail.followup.state === "closed" ? "恢复自动判断" : "关闭本线程跟进"}</button>{detail.followup.state === "awaiting_reply" && <button type="button" className="primary" disabled={busy || !status?.autoDraftReady} onClick={() => void runJob(() => api(`/api/mail/threads/${encodeURIComponent(detail.item.threadId)}/followup-draft`, { method: "POST", body: JSON.stringify({ requestId: requestId() }) }), (result) => { const value = result as { status?: string }; return value.status === "drafted" ? "催复草稿已写入 QQ 草稿箱" : value.status === "no_reply" ? "当前线程无需催复" : value.status === "needs_input" ? "催复前需要你补充决定" : "催复草稿处理完成"; })}>生成催复草稿</button>}</div></div>}
    </section>

    <section className="mail-section" aria-labelledby="mail-reminder-title"><div className="mail-section-heading"><div><h3 id="mail-reminder-title">草稿提醒角色</h3><p className="muted">每批新建草稿汇总为一条会话主动消息，并报告处理失败数</p></div><select aria-label="邮件提醒角色" value={reminderRoleId} disabled={busy} onChange={(event) => { const value = event.target.value; setReminderRoleId(value); void run(() => api("/api/mail/reminder-role", { method: "PUT", body: JSON.stringify({ roleId: value || null }) }), () => "邮件提醒角色已更新"); }}><option value="">不发送提醒</option>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></div>
      {reminderBatches.length > 0 && <div className="mail-reminder-batches" aria-label="邮件提醒批次">{reminderBatches.map((batch) => <article className="mail-reminder-batch" key={batch.batchId}><div><strong>{batch.status === "delivered" ? "已提醒" : batch.status === "completed" ? "无需提醒" : batch.status === "sending" ? "投递中" : batch.status === "failed" ? "投递失败" : "等待提醒"}</strong><time>{batch.createdAt ? new Date(batch.createdAt).toLocaleString() : ""}</time></div>{batch.failureCount > 0 && <p>本批有 {batch.failureCount} 封邮件处理失败或待核实。</p>}{batch.lastError && <p className="mail-item-error">{batch.lastError}</p>}{batch.failureSummaries.length > 0 && <ul>{batch.failureSummaries.slice(0, 3).map((summary, index) => <li key={`${index}-${summary}`}>{summary}</li>)}</ul>}</article>)}</div>}
    </section>
  </section>;
}
