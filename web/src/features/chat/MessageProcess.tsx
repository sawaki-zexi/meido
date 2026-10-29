import type { Message } from '../../api/types';
import type { ToolProgressState } from './toolProgress';
import { ChatThinkingBlock } from './ChatThinkingBlock';
import { ChatToolCalls, type ToolEntry } from './ChatToolCalls';

export type ProcessEntry = { type: 'thinking' | 'text'; id: string; text: string }
  | ToolEntry;

export function readProcess(value: unknown): ProcessEntry[] {
  return Array.isArray(value) ? value.filter((item): item is ProcessEntry => item && typeof item.id === 'string'
    && ((item.type === 'thinking' || item.type === 'text') && typeof item.text === 'string'
      || item.type === 'tool' && typeof item.name === 'string' && typeof item.result === 'string' && typeof item.status === 'string'
        && (item.argumentsPreview === undefined || typeof item.argumentsPreview === 'string'))) : [];
}

/** Provider text and bounded tool summaries are plain text, never executable markup. */
export function MessageProcess({ message, progress, stopping }: { message: Message; stopping?: boolean; progress?: ToolProgressState | null }) {
  const process = readProcess(message.metadata?.process);
  const liveThinking = typeof message.metadata?.streamThinking === 'string' ? message.metadata.streamThinking : '';
  const known = new Set(process.filter(item => item.type === 'tool').map(item => item.id));
  const calls = progress?.calls ?? [];
  const entries: ProcessEntry[] = process.map(item => {
    const live = item.type === 'tool' && calls.find(call => call.toolCallId === item.id);
    return live ? { ...item, type: 'tool', id: live.toolCallId, name: live.toolName, status: live.status, result: live.result ?? '',
      argumentsPreview: live.argumentsPreview ?? (item as ToolEntry).argumentsPreview, droppedUpdates: live.droppedUpdates } : item;
  });
  for (const call of calls) if (!known.has(call.toolCallId)) entries.push({ type: 'tool', id: call.toolCallId, name: call.toolName, status: call.status, result: call.result ?? '', argumentsPreview: call.argumentsPreview, droppedUpdates: call.droppedUpdates });
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
      if (first.type === 'thinking') return <ChatThinkingBlock key={first.id} content={first.text} streaming={first.id === 'live-thinking'} />;
      return <ChatToolCalls key={first.id} calls={group as ToolEntry[]} streaming={message.status === 'streaming'}
        closing={progress?.closing ? (stopping ? '正在停止…' : '等待最终状态') : undefined} />;
    })}
  </div>;
}
