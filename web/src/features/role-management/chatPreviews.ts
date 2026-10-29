import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { Message } from '../../api/types';

export type ChatPreview = { content: string; role: string; createdAt: string; hasImages?: boolean; hasFiles?: boolean };
export function messagePreview(message: Message): ChatPreview {
  const attachments = Array.isArray(message.metadata?.attachments) ? message.metadata.attachments : [];
  return { content: message.content, role: message.role, createdAt: message.createdAt,
    hasImages: attachments.some(item => item?.kind === 'image'), hasFiles: attachments.some(item => item?.kind === 'file') };
}
export function previewText(preview?: ChatPreview): string {
  if (!preview) return '';
  const text = preview.content.replace(/```[\s\S]*?```/g, '[代码]').replace(/!\[([^\]]*)\]\([^)]*\)/g, '[图片]')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*_`#>~]/g, '').replace(/\s+/g, ' ').trim();
  const body = text || (preview.hasImages ? '[图片]' : preview.hasFiles ? '[文件]' : '');
  return body ? `${preview.role === 'user' ? '你：' : ''}${body}` : '';
}
export function previewTime(value: string): string {
  const date = new Date(value), now = new Date();
  if (!Number.isFinite(date.getTime())) return '';
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return '昨天';
  return date.toLocaleDateString([], date.getFullYear() === now.getFullYear() ? { month: 'numeric', day: 'numeric' } : { year: 'numeric', month: 'numeric', day: 'numeric' });
}
export function useChatPreviews(roleId: string | null, messages: Message[]) {
  const [previews, setPreviews] = useState<Record<string, ChatPreview>>({});
  useEffect(() => {
    let active = true;
    let loading = false;
    const refresh = async () => {
      if (loading || document.visibilityState === 'hidden') return;
      loading = true;
      try {
        const { previews: fresh } = await api<{ previews: Record<string, ChatPreview> }>('/api/chat/previews');
        if (active) setPreviews(current => {
          const next = { ...fresh };
          for (const [id, item] of Object.entries(current)) if (!next[id] || Date.parse(item.createdAt) > Date.parse(next[id].createdAt)) next[id] = item;
          return next;
        });
      } catch { /* A preview failure must not interrupt chat. */ }
      finally { loading = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    window.addEventListener('focus', refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener('focus', refresh); };
  }, []);
  useEffect(() => {
    if (!roleId) return;
    const last = [...messages].reverse().find(message => message.status === 'completed' && previewText(messagePreview(message)));
    if (last) setPreviews(current => current[roleId] && Date.parse(current[roleId].createdAt) > Date.parse(last.createdAt)
      ? current : { ...current, [roleId]: messagePreview(last) });
  }, [roleId, messages]);
  return previews;
}
