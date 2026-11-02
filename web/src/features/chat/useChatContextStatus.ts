import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { ContextUsage } from './chatContextState';

export type ContextStatus = {
  roleId: string; sessionKey: string; status: 'ready' | 'busy' | 'unavailable'; reason: string;
  inputTokens: number | null; source: ContextUsage['source'] | null; capacity: number | null;
  configurationId: string | null; model: string | null; inputLimit: number | null;
  outputReserve: number | null; safetyMargin: number | null; compactTrigger: number | null; compactTarget: number | null;
  canCompact?: boolean; compacted?: boolean;
};
export type ContextInspection = { status: ContextStatus | null; loading: boolean; busy: boolean; error: string; refresh: () => void; compact?: () => Promise<void>; notice?: string };

/** One mounted saved-context reader: coalesce reads and abandon old identities. */
export function useChatContextStatus({ roleId, sessionKey, sending, enabled, revision, modelKey, sessionSnapshot }: {
  roleId?: string; sessionKey?: string; sending?: boolean; enabled: boolean; revision: string; modelKey: string; sessionSnapshot?: object;
}): ContextInspection {
  const [result, setResult] = useState<{ key: string; identity: string; status: ContextStatus | null; loading: boolean; error: string }>({ key: '', identity: '', status: null, loading: false, error: '' });
  const identity = JSON.stringify([roleId, sessionKey, modelKey]);
  const key = JSON.stringify([identity, revision]);
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const pending = useRef<AbortController | null>(null);
  const operation = useRef<{ scope: string; sequence: number } | null>(null);
  const sequence = useRef(0);
  const scope = JSON.stringify([roleId, sessionKey]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [compacting, setCompacting] = useState(false);
  const [notice, setNotice] = useState('');
  const retried = useRef(false);
  const lastSnapshot = useRef(sessionSnapshot);
  const refresh = useCallback(() => {
    if (!roleId || !sessionKey || !enabled || sending || pending.current || operation.current?.scope === scope) return;
    const controller = new AbortController();
    pending.current = controller;
    setResult(previous => ({ key, identity, status: previous.identity === identity ? previous.status : null, loading: true, error: '' }));
    void api<ContextStatus>(`/api/roles/${encodeURIComponent(roleId)}/context-status`, { signal: controller.signal })
      .then(status => {
        if (controller.signal.aborted) return;
        if (status.roleId !== roleId || status.sessionKey !== sessionKey) throw new Error('上下文来源已变化，请重新检查');
        setResult(previous => {
          const saved = previous.identity === identity ? previous.status : null;
          // Shiori retains busy usage only for the same saved conversation.
          // The composer model identity also clears it before a new read.
          if (status.status === 'busy' && status.inputTokens === null && saved?.inputTokens != null) {
            const { inputTokens, capacity, source, configurationId, model } = saved;
            return { key, identity, status: { ...status, inputTokens, capacity, source, configurationId, model }, loading: false, error: '' };
          }
          return { key, identity, status, loading: false, error: '' };
        });
      }).catch(cause => {
        if (!controller.signal.aborted) setResult({ key, identity, status: null, loading: false, error: errorMessage(cause, '读取当前上下文失败') });
      }).finally(() => { if (pending.current === controller) pending.current = null; });
  }, [enabled, identity, key, roleId, sending, sessionKey, scope]);
  const latestRefresh = useRef(refresh);
  latestRefresh.current = refresh;

  const compact = useCallback(async () => {
    if (!roleId || !sessionKey || !enabled || sending || operation.current?.scope === scope) return;
    pending.current?.abort(); pending.current = null;
    const owner = { scope, sequence: ++sequence.current };
    operation.current = owner;
    setCompacting(true); setNotice('正在压缩上下文');
    try {
      const status = await api<ContextStatus>(`/api/roles/${encodeURIComponent(roleId)}/context-compact`, { method: 'POST' });
      if (operation.current !== owner || currentScope.current !== scope) return;
      if (currentIdentity.current === identity) setResult({ key, identity, status, loading: false, error: '' });
      setNotice(status.reason);
    } catch (cause) {
      if (operation.current === owner && currentScope.current === scope) setNotice(errorMessage(cause, '压缩上下文失败'));
    } finally {
      if (operation.current === owner) {
        operation.current = null;
        if (currentScope.current === scope) {
          setCompacting(false);
          latestRefresh.current();
        }
      }
    }
  }, [roleId, sessionKey, enabled, sending, scope, key, identity]);
  useEffect(() => { setCompacting(false); setNotice(''); return () => { operation.current = null; }; }, [scope]);

  useEffect(() => {
    retried.current = false;
    setResult(previous => previous.identity === identity ? previous : { key, identity, status: null, loading: false, error: '' });
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

  const saved = result.identity === identity ? result.status : null;
  if (sending) return { status: saved, loading: false, busy: true, error: '正在回复或整理上下文', refresh, compact, notice };
  const current = result.key === key ? result : { status: saved, loading: enabled, error: '' };
  return { ...current, busy: compacting || current.status?.status === 'busy', refresh, compact, notice };
}
