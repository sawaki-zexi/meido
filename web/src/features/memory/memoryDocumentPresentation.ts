import { typeLabels } from './types';

type MarkdownNode = {
  type: string; value?: string; depth?: number; children?: MarkdownNode[];
  data?: { hName?: string; hProperties?: Record<string, unknown> };
};

const entryLabels: Record<string, string> = { ...typeLabels, user: '主人', assistant: '角色', identity: '基础信息', key_info: '重要信息',
  health_long_term: '健康状况', requested_memory: '主人嘱咐', correction: '补充与更正' };

function messageRange(source: string): string | null {
  const match = /^(?:consolidation|listening|group-owner):[^\s<>]+?:(\d+)-(\d+):[a-f\d]{12,64}(?::[^\s<>]+)?$/i.exec(source);
  if (!match) return null;
  const start = Number(match[1]), end = Number(match[2]);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return null;
  return start === end ? `消息 ${start}` : `消息 ${start}–${end}`;
}

function documentNote(value: string): MarkdownNode {
  return { type: 'paragraph', data: { hName: 'div', hProperties: { className: 'memory-document-source' } }, children: [{ type: 'text', value }] };
}

function sourceNote(range: string): MarkdownNode {
  return documentNote(`来源：${range}`);
}

function cutoffLabel(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    const date = new Date(value), day = new Date(value.slice(0, 10));
    if (!Number.isNaN(date.getTime()) && !Number.isNaN(day.getTime()) && day.toISOString().slice(0, 10) === value.slice(0, 10)) {
      return `整理至：${date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}`;
    }
  }
  return '整理时间未记录';
}

/** Project reserved host markers after Markdown parsing, so code and quotations stay intact. */
export function remarkMemoryDocument({ recent = false }: { recent?: boolean } = {}) {
  return (tree: MarkdownNode) => {
    function project(node: MarkdownNode) {
      if (!node.children || node.type === 'blockquote') return;
      if (node.type === 'heading' && node.depth) node.depth = Math.min(node.depth + 2, 6);
      if (node.type === 'listItem') {
        const paragraph = node.children[0];
        const first = paragraph?.type === 'paragraph' ? paragraph.children?.[0] : undefined;
        const tag = first?.type === 'text' ? /^\[([^\]]+)\] /.exec(first.value ?? '') : null;
        if (tag && entryLabels[tag[1]]) {
          first!.value = first!.value!.slice(tag[0].length);
          paragraph.children!.unshift({ type: 'strong', children: [{ type: 'text', value: entryLabels[tag[1]] + ' · ' }] });
        }
        const last = paragraph?.type === 'paragraph' ? paragraph.children?.at(-1) : undefined;
        const suffix = last?.type === 'text' ? /\s+[（(]来源：整理 (consolidation:[^\s()（）<>]+)[）)]\s*$/.exec(last.value ?? '') : null;
        const range = suffix ? messageRange(suffix[1]) : null;
        if (suffix && range) {
          last!.value = last!.value!.slice(0, suffix.index);
          node.children.splice(1, 0, sourceNote(range));
        }
      }
      node.children = node.children.flatMap((child, index) => {
        const previous = node.children![index - 1];
        if (recent && node.type === 'root' && child.type === 'paragraph' && child.children?.length === 1 && child.children[0].type === 'text'
          && previous?.type === 'heading' && previous.children?.length === 1 && previous.children[0].value === '最近聊过的事') {
          const cutoff = /^until: (.*)$/.exec(child.children[0].value ?? '');
          if (cutoff) return [documentNote(cutoffLabel(cutoff[1]))];
        }
        // Host comments are standalone blocks; inline HTML remains ordinary document text.
        if (child.type === 'html' && (node.type === 'root' || node.type === 'listItem')) {
          const value = child.value?.trim() ?? '';
          const source = /^<!-- source: ([^\r\n<>]+) -->$/.exec(value);
          if (source) return [...new Set(source[1].split(',').map(key => messageRange(key.trim())).filter((range): range is string => Boolean(range)))].map(sourceNote);
          if (/^<!-- (?:meido-optimizer:(?:start|end)|recent-raw|recent-context-sources: [\s\S]*|a-preview = assistant reply preview only) -->$/.test(value)) return [];
        }
        project(child);
        return [child];
      });
      if (node.type === 'listItem') {
        const sources = new Set<string>();
        node.children = node.children.filter(child => {
          if (child.data?.hProperties?.className !== 'memory-document-source') return true;
          const label = child.children?.[0]?.value ?? '';
          if (sources.has(label)) return false;
          sources.add(label);
          return true;
        });
      }
    }
    project(tree);
    if (!tree.children?.length) tree.children = [{ type: 'paragraph', data: { hProperties: { className: 'memory-document-empty' } }, children: [{ type: 'text', value: '这里还没有内容' }] }];
  };
}
