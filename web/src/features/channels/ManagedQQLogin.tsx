import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Notice } from "../../ui/Status";
import type { Account, ManagedQQSession } from "./channelAccounts";

const preparationLabel: Record<string, string> = {
  idle: "等待准备", downloading: "正在下载 QQ 连接组件", extracting: "正在解压连接组件",
  verifying: "正在检查连接组件", ready: "连接组件已就绪", error: "准备失败",
};

export function ManagedQQLogin({ roleId, existingAccount, canConnect, getRules, onConnected }: {
  roleId: string;
  existingAccount?: { managedRef: string; accountId: string };
  canConnect: boolean;
  getRules: () => Record<string, unknown>;
  onConnected: (account: Account) => void;
}) {
  const [session, setSession] = useState<ManagedQQSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dismissedSessionError, setDismissedSessionError] = useState("");
  const mounted = useRef(true);
  const generation = useRef(0);
  const temporaryRef = useRef<string | null>(null);
  const committing = useRef(false);
  const allowed = useRef(canConnect);
  allowed.current = canConnect;
  const base = `/api/roles/${encodeURIComponent(roleId)}/channels/qq/managed`;
  const existingRef = existingAccount?.managedRef;

  const discard = (ref: string) => api(`${base}/${encodeURIComponent(ref)}`, { method: "DELETE" });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      const ref = temporaryRef.current;
      if (ref && !committing.current) void discard(ref).catch(() => undefined);
    };
    // The parent keys this component by role and account.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function load(path: string, init?: RequestInit, createsSession = false) {
    const sequence = ++generation.current;
    setBusy(true); setError(""); setDismissedSessionError("");
    try {
      const result = await api<ManagedQQSession>(path, init);
      if (!mounted.current || generation.current !== sequence || !allowed.current) {
        if (createsSession) await discard(result.ref);
        return;
      }
      if (createsSession) temporaryRef.current = result.ref;
      setSession(result);
    } catch (cause) {
      if (mounted.current && sequence === generation.current) setError(errorMessage(cause, "无法读取 QQ 登录状态"));
    } finally {
      if (mounted.current && sequence === generation.current) setBusy(false);
    }
  }

  useEffect(() => {
    if (!existingRef || !canConnect) return;
    void load(`${base}/${encodeURIComponent(existingRef)}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingRef, canConnect]);

  useEffect(() => {
    if (!session || !canConnect || session.login.phase === "online" || (!existingRef && (session.login.phase === "error" || session.preparation.stage === "error")) || error) return;
    let cancelled = false;
    const sequence = generation.current;
    const timeout = window.setTimeout(() => {
      void api<ManagedQQSession>(`${base}/${encodeURIComponent(session.ref)}`).then((result) => {
        if (!cancelled && mounted.current && sequence === generation.current) setSession(result);
      }).catch((cause) => {
        if (!cancelled && mounted.current && sequence === generation.current) setError(errorMessage(cause, "QQ 登录状态读取失败，可刷新重试"));
      });
    }, 1500);
    return () => { cancelled = true; window.clearTimeout(timeout); };
  }, [base, canConnect, error, existingRef, session]);

  async function cancel() {
    const ref = temporaryRef.current;
    generation.current += 1;
    setBusy(true); setError("");
    try {
      if (ref) await discard(ref);
      temporaryRef.current = null;
      if (mounted.current) setSession(null);
      return true;
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause, "取消 QQ 登录失败，请重试"));
      return false;
    } finally { if (mounted.current) setBusy(false); }
  }

  async function retry() {
    if (await cancel() && mounted.current && allowed.current) await load(`${base}/begin`, { method: "POST" }, true);
  }

  useEffect(() => {
    if (!canConnect) {
      generation.current += 1;
      setBusy(false);
      if (temporaryRef.current) void cancel();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canConnect]);

  async function confirm() {
    if (!session?.login.uin || !canConnect) return;
    const ref = session.ref;
    const sequence = ++generation.current;
    committing.current = true;
    setBusy(true); setError("");
    try {
      const result = existingAccount
        ? await api<{ account: Account }>(`/api/roles/${encodeURIComponent(roleId)}/channels/accounts/${encodeURIComponent(existingAccount.accountId)}/state`, { method: "POST", body: JSON.stringify({ enabled: true }) })
        : await api<{ account: Account }>(`${base}/${encodeURIComponent(ref)}/commit`, { method: "POST", body: JSON.stringify(getRules()) });
      temporaryRef.current = null;
      if (mounted.current && sequence === generation.current) onConnected(result.account);
    } catch (cause) {
      if (mounted.current && sequence === generation.current) setError(errorMessage(cause, "确认 QQ 连接失败"));
    } finally {
      committing.current = false;
      if (!mounted.current && temporaryRef.current) void discard(temporaryRef.current).catch(() => undefined);
      if (mounted.current && sequence === generation.current) setBusy(false);
    }
  }

  const loginReady = session?.preparation.stage === "ready" && session.login.phase !== "error";
  const transitionMessage = loginReady && !session.login.error && session.login.phase !== "online"
    ? session.login.loginPhase === "qrcode_scanned" ? "二维码已扫描，请在手机 QQ 上确认登录…"
      : session.login.loginPhase === "initializing" ? "正在完成 QQ 登录，请稍候…" : null
    : null;
  const sessionError = session?.preparation.stage === "error" ? session.preparation.error
    : session?.preparation.stage === "ready" ? session.login.error : null;
  const sessionErrorKey = `${session?.ref}:${sessionError}`;

  return <section className="managed-qq-login" aria-label="QQ 扫码登录">
    {!session && <><p>使用 QQ 扫码登录，Meido 会准备并管理连接。</p><button type="button" className="primary" disabled={busy || !canConnect} onClick={() => void load(existingRef ? `${base}/${encodeURIComponent(existingRef)}` : `${base}/begin`, existingRef ? undefined : { method: "POST" }, !existingRef)}>{busy ? "正在准备…" : existingRef ? "读取登录状态" : "准备并扫码登录"}</button></>}
    {error && <><Notice onDismiss={() => setError("")}>{error}</Notice>{session && <button type="button" disabled={busy || !canConnect} onClick={() => void load(`${base}/${encodeURIComponent(session.ref)}`)}>重试读取状态</button>}</>}
    {session && <>
      <p role="status">{preparationLabel[session.preparation.stage] ?? "正在准备"}{session.preparation.stage !== "ready" && session.preparation.stage !== "error" ? ` · ${session.preparation.percent}%` : ""}</p>
      {session.preparation.stage !== "ready" && session.preparation.stage !== "error" && <progress max={100} value={session.preparation.percent} aria-label="QQ 连接组件准备进度" />}
      {sessionError && dismissedSessionError !== sessionErrorKey && <Notice onDismiss={() => setDismissedSessionError(sessionErrorKey)}>{sessionError}</Notice>}
      {loginReady && session.login.qrcode && <><img className="managed-qq-qrcode" src={session.login.qrcode} alt="QQ 登录二维码" />{!transitionMessage && <p>使用手机 QQ 扫码，并在手机上确认登录。</p>}</>}
      {transitionMessage && <p role="status">{transitionMessage}</p>}
      {loginReady && session.login.phase === "starting" && <p role="status">正在启动 QQ，等待登录二维码…</p>}
      {loginReady && !transitionMessage && session.login.phase === "login_required" && !session.login.qrcode && !session.login.error && <p role="status">等待登录二维码，可刷新二维码重试。</p>}
      {existingRef && session.login.phase === "stopped" && <p role="status">QQ 尚未启动，请点击上方的连接按钮。</p>}
      {loginReady && session.login.uin && <p role="status">已登录：{session.login.nickname || "QQ"}（{session.login.uin}）</p>}
      <div className="account-actions">
        {!existingRef && <button type="button" disabled={busy} onClick={() => void cancel()}>取消登录</button>}
        {!existingRef && (session.preparation.stage === "error" || session.login.phase === "error")
          ? <button type="button" disabled={busy || !canConnect} onClick={() => void retry()}>重试登录</button>
          : loginReady && session.login.phase === "login_required" && <button type="button" disabled={busy || !canConnect || Boolean(transitionMessage)} onClick={() => void load(`${base}/${encodeURIComponent(session.ref)}/refresh`, { method: "POST" })}>刷新二维码</button>}
        {loginReady && session.login.phase === "online" && session.login.uin && <button type="button" className="primary" disabled={busy || !canConnect} onClick={() => void confirm()}>确认连接</button>}
      </div>
    </>}
  </section>;
}
