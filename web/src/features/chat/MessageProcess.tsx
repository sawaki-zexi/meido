import type { Message } from '../../api/types';
import type { ToolProgressState } from './toolProgress';

export type ProcessEntry = { type: 'thinking' | 'text'; id: string; text: string }
  | { type: 'tool'; id: string; name: string; status: string; result: string };
const labels: Record<string, string> = { running: '运行中', succeeded: '已完成', failed: '失败', unknown: '结果不确定', denied: '未获许可', invalid_arguments: '参数无效', timed_out: '已超时', cancelled: '已取消' };

export function readProcess(value: unknown): ProcessEntry[] {
  return Array.isArray(value) ? value.filter((item): item is ProcessEntry => item && typeof item.id === 'string'
    && ((item.type === 'thinking' || item.type === 'text') && typeof item.text === 'string'
      || item.type === 'tool' && typeof item.name === 'string' && typeof item.result === 'string' && typeof item.status === 'string')) : [];
}

/** Provider text and bounded tool summaries are plain text, never executable markup. */
export function MessageProcess({ message, progress, stopping }: { message: Message; stopping?: boolean; progress?: ToolProgressState | null }) {
  const process = readProcess(message.metadata?.process);
  const liveThinking = typeof message.metadata?.streamThinking === 'string' ? message.metadata.streamThinking : '';
  const known = new Set(process.filter(item => item.type === 'tool').map(item => item.id));
  const calls = progress?.calls ?? [];
  const entries: ProcessEntry[] = process.map(item => {
    const live = item.type === 'tool' && calls.find(call => call.toolCallId === item.id);
    return live ? { type: 'tool', id: live.toolCallId, name: live.toolName, status: live.status, result: live.result ?? '' } : item;
  });
  for (const call of calls) if (!known.has(call.toolCallId)) entries.push({ type: 'tool', id: call.toolCallId, name: call.toolName, status: call.status, result: call.result ?? '' });
  if (liveThinking) entries.push({ type: 'thinking', id: 'live-thinking', text: liveThinking });
  if (!entries.length) return null;
  // Consecutive tools share a foldout; reasoning between turns keeps its place.
  const groups: ProcessEntry[][] = [];
  for (const entry of entries) {
    if (entry.type === 'tool' && groups.at(-1)?.[0].type === 'tool') groups[groups.length - 1].push(entry);
    else groups.push([entry]);
  }
  return <div className="message-process">
    {groups.map(group => {
      const first = group[0];
      if (first.type === 'text') return <p className="process-text" key={first.id}>{first.text}</p>;
      if (first.type === 'thinking') return <details className="thinking-block" key={first.id} open={first.id === 'live-thinking' || undefined}><summary>{first.id === 'live-thinking' ? '思考中' : '思考过程'}</summary><p>{first.text}</p></details>;
      const running = group.some(item => item.type === 'tool' && item.status === 'running');
      return <details className="tool-block" key={first.id} open={Boolean(progress) || running || undefined}>
        <summary>{group.length} 次工具调用{running ? ' · 运行中' : ''}</summary>
        <div role={progress ? 'status' : undefined} aria-label="工具执行"><ol>{group.map(item => item.type === 'tool' && <li key={item.id}>
          <div className="chat-tool-heading"><strong>{item.name}</strong><span data-state={item.status}>{item.status === 'running' && progress?.closing ? (stopping ? '正在停止…' : '等待最终状态') : labels[item.status] ?? '结果不确定'}</span></div>
          {item.result && <p>{item.result}</p>}
          {calls.filter(call => call.toolCallId === item.id && call.droppedUpdates > 0).map(call => <small key={call.toolCallId}>已合并 {call.droppedUpdates} 次中间更新，仅显示最新摘要</small>)}
        </li>)}</ol></div>
      </details>;
    })}
  </div>;
}
