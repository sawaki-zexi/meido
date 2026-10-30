// Adapted from Shiori-Agent d149de7, ChatToolCalls.tsx (MIT; see third_party/shiori-agent).
import { useEffect, useId, useRef, useState } from 'react';
import { CaretDown } from '@phosphor-icons/react/dist/csr/CaretDown';
import { CircleNotch } from '@phosphor-icons/react/dist/csr/CircleNotch';
import { FileText } from '@phosphor-icons/react/dist/csr/FileText';
import { MagnifyingGlass } from '@phosphor-icons/react/dist/csr/MagnifyingGlass';
import { PencilSimple } from '@phosphor-icons/react/dist/csr/PencilSimple';
import { Terminal } from '@phosphor-icons/react/dist/csr/Terminal';
import { Warning } from '@phosphor-icons/react/dist/csr/Warning';
import { Wrench } from '@phosphor-icons/react/dist/csr/Wrench';

export type ToolEntry = { type: 'tool'; id: string; name: string; status: string; result: string; argumentsPreview?: string; droppedUpdates?: number };
const labels: Record<string, string> = { running: '运行中', succeeded: '已完成', failed: '失败', unknown: '结果不确定', denied: '未获许可', invalid_arguments: '参数无效', timed_out: '已超时', cancelled: '已取消' };

function toolIcon(name: string) {
  if (/search/.test(name)) return MagnifyingGlass;
  if (/read|file/.test(name)) return FileText;
  if (/write|edit/.test(name)) return PencilSimple;
  if (/shell|command/.test(name)) return Terminal;
  return Wrench;
}

export function ChatToolCalls({ calls, streaming, closing }: { calls: ToolEntry[]; streaming: boolean; closing?: string }) {
  const [expanded, setExpanded] = useState(streaming);
  const [expandedCalls, setExpandedCalls] = useState<Set<string>>(() => new Set());
  const previousStreaming = useRef(streaming);
  const id = useId();
  useEffect(() => {
    if (previousStreaming.current && !streaming) {
      setExpanded(false);
      setExpandedCalls(new Set());
    } else if (!previousStreaming.current && streaming) setExpanded(true);
    previousStreaming.current = streaming;
  }, [streaming]);
  const running = calls.filter(call => call.status === 'running').length;
  return <div className="chat-tool-calls" role={streaming ? 'status' : undefined} aria-label="工具执行">
    <button type="button" className="chat-tool-toggle" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(value => !value)}>
      <CaretDown size={13} className={expanded ? 'is-expanded' : ''} aria-hidden /><Wrench size={13} aria-hidden />
      <span>{calls.length} 次工具调用</span>
      {running > 0 && <span className="chat-tool-running-count">{closing ?? `${running} 个执行中`}</span>}
    </button>
    <div id={id} className={`chat-tool-call-content${expanded ? ' is-expanded' : ''}`} aria-hidden={!expanded}>
      <div><div className="chat-tool-rows">{calls.map((call, index) => {
        const opened = expandedCalls.has(call.id);
        const runningCall = call.status === 'running';
        const failed = !runningCall && call.status !== 'succeeded';
        const StatusIcon = runningCall ? CircleNotch : failed ? Warning : toolIcon(call.name);
        const status = labels[call.status] ?? labels.unknown;
        const detailId = `${id}-${index}`;
        const argumentsPreview = call.argumentsPreview || '参数未记录';
        return <div className="chat-tool-call" key={call.id}>
          <button type="button" className="chat-tool-row" tabIndex={expanded ? 0 : -1} aria-expanded={opened} aria-controls={detailId}
            aria-label={`${call.name} · ${status}`} aria-describedby={`${detailId}-arguments`} onClick={() => setExpandedCalls(previous => {
              const next = new Set(previous);
              if (next.has(call.id)) next.delete(call.id); else next.add(call.id);
              return next;
            })}>
            <span className={`chat-tool-icon${runningCall ? ' is-running' : ''}`}>
              <StatusIcon size={13} weight={failed ? 'fill' : 'regular'} className={runningCall ? 'chat-tool-running' : 'chat-tool-status-icon'} aria-hidden />
              {!runningCall && <CaretDown size={12} className={`chat-tool-hover-caret${opened ? ' is-expanded' : ''}`} aria-hidden />}
            </span>
            <span className="chat-tool-name">{call.name}</span>
            <span id={`${detailId}-arguments`} className="chat-tool-arguments" title={argumentsPreview}>{argumentsPreview}</span>
          </button>
          <div id={detailId} className={`chat-tool-call-detail${opened ? ' is-expanded' : ''}`} aria-hidden={!expanded || !opened}>
            <div><div className="chat-tool-result" data-state={failed ? 'failed' : undefined}>
              <pre>{call.result || '暂无结果'}</pre>
              {!!call.droppedUpdates && <small>已合并 {call.droppedUpdates} 次中间更新，仅显示最新摘要</small>}
            </div></div>
          </div>
        </div>;
      })}</div></div>
    </div>
  </div>;
}
