import { useEffect, useRef, useState } from 'react';
import { errorDetail, errorMessage } from '../../api/client';

export type AdvancedValues = Record<string, number | boolean>;
type Snapshot = { revision: number; values: AdvancedValues };
type Operation = { expectedRevision: number; operationId: string; values: AdvancedValues; raw: Record<string, string | boolean> };

export function useAdvancedDraft() {
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [status, setStatus] = useState('正在读取…');
  const [problem, setProblem] = useState('');
  const [loaded, setLoaded] = useState(false);
  const state = useRef({ snapshot: null as Snapshot | null, draft: {} as Record<string, string | boolean>,
    dirty: new Set<string>(), operation: null as Operation | null, running: false,
    blocked: '', readback: false, mounted: true });
  const publish = () => { if (state.current.mounted) setDraft({ ...state.current.draft }); };
  const accept = (snapshot: Snapshot, operation?: Operation) => {
    const current = state.current;
    if (operation) for (const key of Object.keys(operation.values)) {
      if (current.draft[key] === operation.raw[key]) current.dirty.delete(key);
    }
    current.snapshot = snapshot;
    for (const [key, value] of Object.entries(snapshot.values)) if (!current.dirty.has(key)) current.draft[key] = typeof value === 'boolean' ? value : String(value);
    publish();
  };
  async function read() {
    const response = await fetch('/api/settings/advanced');
    if (!response.ok) throw new Error(errorDetail(await response.json(), response.status));
    return await response.json() as Snapshot;
  }
  const save = async () => {
    const current = state.current;
    if (current.running || current.blocked || !current.snapshot) return;
    current.running = true;
    try {
      while (current.dirty.size || current.operation) {
        if (!current.operation) {
          const values: AdvancedValues = {};
          const raw: Operation['raw'] = {};
          for (const key of current.dirty) {
            const value = current.draft[key];
            if (typeof value === 'string' && (!value.trim() || !Number.isFinite(Number(value)))) {
              current.blocked = 'invalid';
              if (current.mounted) { setProblem('请输入完整数字；草稿已保留。'); setStatus('尚未保存'); }
              return;
            }
            raw[key] = value;
            values[key] = typeof value === 'boolean' ? value : Number(value);
          }
          current.operation = { expectedRevision: current.snapshot.revision, operationId: crypto.randomUUID(), values, raw };
        }
        if (current.mounted) { setStatus('正在保存…'); setProblem(''); }
        const operation = current.operation;
        let snapshot: Snapshot;
        if (current.readback) {
          snapshot = await read();
        } else {
          const response = await fetch('/api/settings/advanced', { method: 'PUT', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ expectedRevision: operation.expectedRevision, operationId: operation.operationId, values: operation.values }) });
          if (!response.ok) {
            current.blocked = response.status === 409 ? 'conflict' : [400, 422].includes(response.status) ? 'invalid' : 'unknown';
            if (current.blocked !== 'unknown') current.operation = null;
            throw new Error(errorDetail(await response.json(), response.status));
          }
          current.readback = true; // A confirmed write is never replayed because its response body was lost.
          snapshot = await response.json() as Snapshot;
        }
        accept(snapshot, operation);
        current.operation = null;
        current.readback = false;
      }
      if (current.mounted) setStatus('已保存');
    } catch (error) {
      current.blocked ||= 'unknown';
      if (current.mounted) { setStatus('尚未完成保存'); setProblem(errorMessage(error, '保存失败，草稿已保留')); }
    } finally { current.running = false; }
  };
  useEffect(() => {
    state.current.mounted = true;
    void read().then(snapshot => { if (state.current.mounted) { accept(snapshot); setLoaded(true); setStatus('设置自动保存'); } })
      .catch(error => { if (state.current.mounted) setProblem(errorMessage(error, '无法读取高级设置')); });
    return () => { state.current.mounted = false; void save(); };
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { void save(); }, 350);
    return () => window.clearTimeout(timer);
  }, [draft]);
  return { draft, status, problem, loaded,
    change(key: string, value: string | boolean) {
      const current = state.current;
      current.draft[key] = value;
      current.dirty.add(key);
      if (current.blocked === 'invalid') current.blocked = '';
      setStatus(current.blocked ? '尚未完成保存' : current.running ? '正在保存，后续修改已排队' : '等待保存…');
      publish();
    },
    retry() { if (state.current.blocked !== 'conflict') { state.current.blocked = ''; void save(); } },
    async reload() {
      try {
        const snapshot = await read();
        state.current.dirty.clear(); state.current.operation = null; state.current.readback = false; state.current.blocked = '';
        accept(snapshot); setLoaded(true); setStatus('设置自动保存'); setProblem('');
      } catch (error) { setProblem(errorMessage(error, '重新读取失败')); }
    },
  };
}
