import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "../../api/client";
import { subscribeRoleRun, type ChatPhase, type ChatRunSnapshot, type StreamEvent } from "../../api/stream";
import type { Message, Role, Session } from "../../api/types";
import { applyToolProgress, closeToolProgress, createToolProgress, type ToolProgressState } from "./toolProgress";

type SessionData = { session: Session; messages: Message[]; activeRun?: ChatRunSnapshot | null };
type RunData = SessionData & { run: ChatRunSnapshot };
type RunReceipt = { snapshotPending: true; run: ChatRunSnapshot };
/** Only a confirmed rejection may restore the submitted draft. */
export type ChatSendResult = boolean | 'detached';
type ActiveReply = { controller: AbortController; roleId: string; sessionKey: string; runId: string; sequence: number;
  closed: boolean; accepted: boolean; admission: Promise<boolean> | null; admissionController?: AbortController;
  admissionDone?: boolean; subscribing?: boolean; progressClosed?: boolean; stopPromise: Promise<void> | null };
const terminal = (run: ChatRunSnapshot) => !['queued', 'created', 'running'].includes(run.status);
const runPath = (reply: ActiveReply) => `/api/roles/${encodeURIComponent(reply.roleId)}/runs/${encodeURIComponent(reply.runId)}`;
const waitForRecovery = (signal: AbortSignal) => new Promise<void>(resolve => {
  if (signal.aborted) { resolve(); return; }
  const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
  const timer = setTimeout(done, 500);
  signal.addEventListener('abort', done, { once: true });
});

function liveMessages(messages: Message[], run: ChatRunSnapshot): Message[] {
  if (terminal(run) || messages.some(message => message.role === 'assistant' && message.runId === run.runId)) return messages;
  // Tool-call rows are deliberately hidden from chat bubbles. On re-entry their
  // bounded process projection still needs a bubble until the next text row.
  return [...messages, { id: `live:${run.runId}`, role: 'assistant', content: '', status: 'streaming',
    sessionKey: run.sessionKey, runId: run.runId, sequence: Math.max(0, ...messages.map(item => item.sequence)) + 1,
    createdAt: new Date().toISOString(), metadata: { process: run.process ?? [] } }];
}

/** Applies one stream event to the message list; kept pure so ordering is easy to reason about. */
export function applyStreamEvent(messages: Message[], event: StreamEvent, sessionKey: string): Message[] {
  switch (event.type) {
    case "user_message_accepted":
      return [...messages.filter(message => message.id !== event.message.id), event.message].sort((a, b) => a.sequence - b.sequence);
    case "assistant_generation_started": {
      const previous = messages.find(message => message.status === 'streaming' && message.runId === event.runId);
      return [...messages.filter(message => message.id !== event.messageId && !(message.status === "streaming" && message.runId === event.runId)), { id: event.messageId, sessionKey, sequence: event.messageSequence ?? messages.length + 1, role: "assistant", content: "", status: "streaming", createdAt: new Date().toISOString(), runId: event.runId, metadata: previous?.metadata }];
    }
    case "assistant_delta":
      return messages.map((message) => message.id === event.messageId ? { ...message, content: message.content + event.delta } : message);
    case 'assistant_thinking_delta':
      return messages.map(message => message.id === event.messageId ? { ...message, metadata: { ...message.metadata,
        streamThinking: `${typeof message.metadata?.streamThinking === 'string' ? message.metadata.streamThinking : ''}${event.delta}`.slice(0, 16384) } } : message);
    case 'assistant_presentation':
      return messages.map(message => message.status === 'streaming' && message.runId === event.runId ? { ...message,
        metadata: { ...message.metadata, process: event.process, streamThinking: '', contextUsage: event.contextUsage } } : message);
    case "assistant_message_committed":
    case "assistant_completed":
    case "assistant_failed":
      // Tool pushes and later natural-chat segments may have no placeholder.
      // Upsert by durable ID; only terminal events close the active run below.
      return [...messages.filter(message => message.id !== event.message.id && !(message.status === 'streaming' && message.runId === event.runId)), event.message]
        .sort((first, second) => first.sequence - second.sequence);
    default:
      return messages;
  }
}

/** The selected view owns a subscription. The server owns accepted execution. */
export function useChat() {
  const [roleId, setRoleId] = useState<string | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [opening, setOpening] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [phase, setPhase] = useState<ChatPhase | null>(null);
  const [toolProgress, setToolProgress] = useState<ToolProgressState | null>(null);
  const activeReply = useRef<ActiveReply | null>(null);
  const openingRequest = useRef<AbortController | null>(null);
  const sourceRevision = useRef(0);
  const refreshRequestId = useRef(0);
  const mounted = useRef(true);
  const current = (reply: ActiveReply) => mounted.current && activeReply.current === reply && !reply.controller.signal.aborted;

  const detach = () => {
    activeReply.current?.controller.abort();
    activeReply.current = null;
  };
  const cancelOpening = () => {
    openingRequest.current?.abort();
    openingRequest.current = null;
  };
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; sourceRevision.current += 1; cancelOpening(); detach(); };
  }, []);

  const finish = (reply: ActiveReply) => {
    if (!current(reply)) return;
    reply.closed = true;
    detach();
    setSending(false); setStopping(false); setToolProgress(null); setPhase(null);
  };

  const applySnapshot = (reply: ActiveReply, data: RunData) => {
    if (!current(reply) || data.session.roleId !== reply.roleId || data.session.sessionKey !== reply.sessionKey
      || data.run.runId !== reply.runId || data.run.roleId !== reply.roleId || data.run.sessionKey !== reply.sessionKey) return false;
    reply.accepted = true;
    reply.sequence = data.run.sequence;
    setError('');
    setSession(data.session); setMessages(liveMessages(data.messages, data.run));
    setPhase(data.run.phase); setStopping(Boolean(reply.stopPromise) || data.run.stopping);
    setToolProgress({ ...createToolProgress(reply), sequence: data.run.sequence, closing: Boolean(reply.progressClosed) || data.run.stopping, calls: data.run.tools });
    if (terminal(data.run)) {
      if (data.run.status !== 'completed' && data.run.status !== 'cancelled') setError(data.run.error || '回复未能完成');
      finish(reply);
    } else setSending(true);
    return true;
  };

  const recover = async (reply: ActiveReply) => {
    const data = await api<RunData>(runPath(reply), { signal: reply.controller.signal });
    return applySnapshot(reply, data);
  };

  const receive = async (reply: ActiveReply, event: StreamEvent): Promise<boolean> => {
    if (!current(reply) || reply.closed || event.runId !== reply.runId) return false;
    if ('toolCallId' in event && (event.roleId !== undefined && event.roleId !== reply.roleId
      || event.sessionKey !== undefined && event.sessionKey !== reply.sessionKey)) return false;
    if ('message' in event && event.message.sessionKey !== reply.sessionKey) return false;
    if (event.type === 'run_snapshot') {
      if (event.run.roleId !== reply.roleId || event.run.sessionKey !== reply.sessionKey) return false;
      // Ring-buffer gaps and server restart recover authoritative history, then
      // discard events at/below the atomically captured snapshot cursor.
      return await recover(reply);
    }
    if (event.sequence === undefined || event.sequence <= reply.sequence) return false;
    if (event.sequence !== reply.sequence + 1) {
      await recover(reply);
      if (!current(reply) || event.sequence <= reply.sequence) return false;
      if (event.sequence !== reply.sequence + 1) throw new Error('回复进度正在重新同步');
    }
    reply.sequence = event.sequence;
    if ('toolCallId' in event) {
      setToolProgress(value => applyToolProgress(value ?? createToolProgress(reply), event));
      return true;
    }
    if (event.type === 'assistant_phase') setPhase(event.phase);
    else setMessages(value => applyStreamEvent(value, event, reply.sessionKey));
    if (event.type === 'assistant_failed' && !reply.stopPromise) setError(event.error || '回复未能完成');
    if (event.type === 'assistant_completed' || event.type === 'assistant_failed') finish(reply);
    return true;
  };

  const subscribe = async (reply: ActiveReply) => {
    if (reply.subscribing) return;
    reply.subscribing = true;
    try {
      while (current(reply) && !reply.closed) {
        try {
          await subscribeRoleRun(reply.roleId, reply.runId, reply.sequence, event => receive(reply, event), reply.controller.signal);
          if (!current(reply) || reply.closed) break;
          await recover(reply);
        } catch (cause) {
          if (!current(reply)) break;
          setError(errorMessage(cause, '连接中断，正在恢复回复'));
        }
        if (!current(reply) || reply.closed) break;
        // Reconnect only the accepted ID. Never retry the original message POST.
        await waitForRecovery(reply.controller.signal);
        if (current(reply)) {
          try { await recover(reply); if (current(reply)) setError(''); } catch { /* Retry until the view detaches. */ }
        }
      }
    } finally { reply.subscribing = false; }
  };

  const attach = (run: ChatRunSnapshot) => {
    detach();
    const reply: ActiveReply = { controller: new AbortController(), roleId: run.roleId, sessionKey: run.sessionKey,
      runId: run.runId, sequence: run.sequence, closed: false, accepted: true, admission: null, stopPromise: null };
    activeReply.current = reply;
    setSending(true); setStopping(run.stopping); setPhase(run.phase);
    setToolProgress({ ...createToolProgress(reply), sequence: run.sequence, closing: run.stopping, calls: run.tools });
    void subscribe(reply);
  };

  const open = async (role: Role): Promise<Message[] | null> => {
    cancelOpening();
    detach();
    const controller = new AbortController();
    openingRequest.current = controller;
    const revision = ++sourceRevision.current;
    setToolProgress(null); setPhase(null); setSending(false); setStopping(false); setError(''); setOpening(true);
    try {
      let data: SessionData;
      for (;;) {
        if (!mounted.current || sourceRevision.current !== revision) return null;
        try {
          data = await api<SessionData>(`/api/roles/${encodeURIComponent(role.id)}/session`, { signal: controller.signal });
          break;
        } catch (cause) {
          if (!(cause instanceof ApiError && cause.status === 503)) throw cause;
          if (!mounted.current || sourceRevision.current !== revision) return null;
          setError(errorMessage(cause, '回复进度正在重新同步'));
          await new Promise<void>(resolve => {
            const done = () => { clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve(); };
            const timer = setTimeout(done, 1000);
            controller.signal.addEventListener('abort', done, { once: true });
          });
        }
      }
      if (!mounted.current || sourceRevision.current !== revision || data.session.roleId !== role.id) return null;
      setError('');
      setRoleId(role.id); setSession(data.session); setMessages(data.activeRun ? liveMessages(data.messages, data.activeRun) : data.messages);
      if (data.activeRun && !terminal(data.activeRun) && data.activeRun.roleId === role.id
        && data.activeRun.sessionKey === data.session.sessionKey) attach(data.activeRun);
      return data.messages;
    } catch (cause) {
      if (mounted.current && sourceRevision.current === revision) setError(errorMessage(cause, '无法打开会话'));
      return null;
    } finally {
      if (openingRequest.current === controller) openingRequest.current = null;
      if (mounted.current && sourceRevision.current === revision) setOpening(false);
    }
  };

  const refresh = useCallback(async () => {
    if (!roleId || activeReply.current || openingRequest.current) return;
    // Focus, visibility and polling reads can overlap within the same role.
    const requestId = ++refreshRequestId.current;
    const revision = sourceRevision.current;
    try {
      const data = await api<SessionData>(`/api/roles/${encodeURIComponent(roleId)}/session`);
      if (!mounted.current || refreshRequestId.current !== requestId || sourceRevision.current !== revision
        || activeReply.current || openingRequest.current || data.session.roleId !== roleId) return;
      setSession(data.session); setMessages(data.activeRun ? liveMessages(data.messages, data.activeRun) : data.messages);
      if (data.activeRun && !terminal(data.activeRun) && data.activeRun.roleId === roleId
        && data.activeRun.sessionKey === data.session.sessionKey) attach(data.activeRun);
    } catch { /* Keep saved history during a temporary refresh failure. */ }
  }, [roleId]);

  const reset = () => {
    cancelOpening();
    detach(); sourceRevision.current += 1;
    setRoleId(null); setSession(null); setMessages([]); setToolProgress(null); setPhase(null);
    setOpening(false); setSending(false); setStopping(false); setError('');
  };

  const send = async (text: string, attachments: string[] = []): Promise<ChatSendResult> => {
    const content = text.trim();
    if ((!content && !attachments.length) || !roleId || activeReply.current || openingRequest.current) return false;
    sourceRevision.current += 1;
    const requestId = crypto.randomUUID();
    const reply: ActiveReply = { controller: new AbortController(), roleId, sessionKey: session?.sessionKey ?? `role:${roleId}`,
      runId: `run-${requestId}`, sequence: 0, closed: false, accepted: false, admission: null,
      admissionController: new AbortController(), stopPromise: null };
    activeReply.current = reply;
    setToolProgress(null); setPhase('preparing'); setError(''); setSending(true); setStopping(false);
    reply.admission = (async () => {
      try {
        const data = await api<RunData | RunReceipt>(`/api/roles/${encodeURIComponent(reply.roleId)}/runs`, {
          method: 'POST', body: JSON.stringify({ content, attachments, requestId }), signal: reply.admissionController?.signal,
        });
        if (!current(reply)) return reply.accepted;
        if ('snapshotPending' in data) {
          reply.accepted = data.run.runId === reply.runId && data.run.roleId === reply.roleId && data.run.sessionKey === reply.sessionKey;
        } else applySnapshot(reply, data);
        if (!reply.accepted) throw new Error('回复接受标识不匹配');
      } catch (cause) {
        if (!current(reply)) return reply.accepted;
        // A missing response is reconciled by the preallocated identity, never
        // matching text or whichever run happens to be current on the server.
        if (cause instanceof ApiError && cause.acceptedRunId === reply.runId) reply.accepted = true;
        if (!reply.accepted && !(cause instanceof ApiError && cause.status < 500)) {
          while (current(reply) && !reply.accepted) {
            try {
              if (await recover(reply)) break;
            } catch (recoveryError) {
              if (!current(reply)) break;
              // A busy exact snapshot with this identity confirms acceptance.
              if (recoveryError instanceof ApiError && recoveryError.acceptedRunId === reply.runId) {
                reply.accepted = true;
                setError('');
                break;
              }
              // Retain the existing exact-read 4xx rejection behavior. A
              // transport error or unconfirmed 5xx does not settle admission.
              if (recoveryError instanceof ApiError && recoveryError.status < 500) break;
            }
            if (!current(reply) || reply.accepted) break;
            setError('发送结果尚未确认，正在恢复连接');
            await waitForRecovery(reply.controller.signal);
          }
        }
        if (!reply.accepted && current(reply)) { setError(errorMessage(cause, '发送失败')); finish(reply); }
      } finally { reply.admissionDone = true; }
      if (reply.accepted && current(reply) && !reply.stopPromise) void subscribe(reply);
      return reply.accepted;
    })();
    return await new Promise<ChatSendResult>((resolve, reject) => {
      // Opening another role keeps the old Composer mounted until its session
      // loads. Detaching must settle it without claiming acceptance/rejection.
      const detached = () => resolve(reply.closed ? reply.accepted : 'detached');
      reply.controller.signal.addEventListener('abort', detached, { once: true });
      void reply.admission!.then(resolve, reject).finally(() => reply.controller.signal.removeEventListener('abort', detached));
      if (reply.controller.signal.aborted) detached();
    });
  };

  const stop = () => {
    const reply = activeReply.current;
    if (!reply || reply.stopPromise) return;
    reply.progressClosed = true;
    setStopping(true);
    setToolProgress(value => value ? closeToolProgress(value) : null);
    reply.stopPromise = (async () => {
      // Even switching the visible role after clicking Stop does not retarget
      // this command. Admission can still be in flight with no response yet.
      let data: SessionData;
      for (;;) {
        try {
          data = await api<SessionData>(runPath(reply) + '/stop', { method: 'POST' });
          reply.accepted = true;
          reply.admissionController?.abort();
          break;
        } catch (cause) {
          if (!(cause instanceof ApiError && cause.status === 404) || reply.admissionDone) throw cause;
          // Stop may race submission admission; only retry this exact identity.
          await new Promise(resolve => setTimeout(resolve, 200));
        }
      }
      if (!current(reply) || data.session.roleId !== reply.roleId || data.session.sessionKey !== reply.sessionKey) return;
      setSession(data.session); setMessages(data.messages); setError(''); finish(reply);
      if (data.activeRun && data.activeRun.runId !== reply.runId && !terminal(data.activeRun)) attach(data.activeRun);
    })();
    void reply.stopPromise.catch(cause => {
      if (current(reply)) {
        setError(errorMessage(cause, '停止回复失败，请重试')); reply.stopPromise = null; setStopping(false);
        // Unknown admission already owns exact-ID recovery; do not start a
        // second recovery/subscription loop until acceptance is confirmed.
        if (reply.accepted) void subscribe(reply);
      }
    });
  };

  return { roleId, session, messages, toolProgress, phase, opening, sending, stopping, error, setError, open, reset, send, stop, refresh };
}

export type ChatState = ReturnType<typeof useChat>;
