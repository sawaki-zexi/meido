import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { ContextUsage } from './ComposerTools';

export type ContextStatus = {
  roleId: string; sessionKey: string; status: 'ready' | 'busy' | 'unavailable'; reason: string;
  inputTokens: number | null; source: ContextUsage['source'] | null; capacity: number | null;
  configurationId: string | null; model: string | null; inputLimit: number | null;
  outputReserve: number | null; safetyMargin: number | null; compactTrigger: number | null; compactTarget: number | null;
};
export type ContextInspection = { status: ContextStatus | null; loading: boolean; busy: boolean; error: string; refresh: () => void };

/** One mounted saved-context reader: coalesce reads and abandon old identities. */
export function useChatContextStatus({ roleId, sessionKey, sending, enabled, revision, modelKey, sessionSnapshot }: {
  roleId?: string; sessionKey?: string; sending?: boolean; enabled: boolean; revision: string; modelKey: string; sessionSnapshot?: object;
}): ContextInspection {
  const [result, setResult] = useState<{ key: string; status: ContextStatus | null; loading: boolean; error: string }>({ key: '', status: null, loading: false, error: '' });
  const key = JSON.stringify([roleId, sessionKey, revision, modelKey]);
  const pending = useRef<AbortController | null>(null);
  const retried = useRef(false);
  const lastSnapshot = useRef(sessionSnapshot);
  const refresh = useCallback(() => {
    if (!roleId || !sessionKey || !enabled || sending || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setResult({ key, status: null, loading: true, error: '' });
    void api<ContextStatus>(`/api/roles/${encodeURIComponent(roleId)}/context-status`, { signal: controller.signal })
      .then(status => {
        if (controller.signal.aborted) return;
        if (status.roleId !== roleId || status.sessionKey !== sessionKey) throw new Error('上下文来源已变化，请重新检查');
        setResult({ key, status, loading: false, error: '' });
      }).catch(cause => {
        if (!controller.signal.aborted) setResult({ key, status: null, loading: false, error: errorMessage(cause, '读取当前上下文失败') });
      }).finally(() => { if (pending.current === controller) pending.current = null; });
  }, [enabled, key, roleId, sending, sessionKey]);

  useEffect(() => {
    retried.current = false;
    refresh();
    return () => { pending.current?.abort(); pending.current = null; };
  }, [refresh]);
  useEffect(() => {
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('focus', visible);
    window.addEventListener('online', refresh);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('focus', visible); window.removeEventListener('online', refresh);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [refresh]);
  // A single deferred read handles the handoff from reply completion to its
  // memory worker. Further reads come from existing lifecycle/user events.
  useEffect(() => {
    if (result.key !== key || result.status?.status !== 'busy' || sending || retried.current) return;
    retried.current = true;
    const timer = window.setTimeout(refresh, 1500);
    return () => window.clearTimeout(timer);
  }, [key, result.key, result.status?.status, refresh, sending]);

  useEffect(() => {
    if (lastSnapshot.current === sessionSnapshot) return;
    lastSnapshot.current = sessionSnapshot;
    // ChatView already refreshes the saved session while idle. Use that event
    // to recover from longer maintenance without a separate polling loop.
    if (result.key === key && result.status?.status === 'busy') refresh();
  }, [sessionSnapshot, key, result.key, result.status?.status, refresh]);

  if (sending) return { status: null, loading: false, busy: true, error: '正在回复或整理上下文，请稍后重试', refresh };
  const current = result.key === key ? result : { status: null, loading: enabled, error: '' };
  return { ...current, busy: current.status?.status === 'busy', refresh };
}
