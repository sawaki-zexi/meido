export type MemoryOrigin = {
  kind: string; sessionKey: string; messageIds: string[]; messageRange: [number, number] | null; stableSourceKey: string;
};
export type Memory = {
  id: string; roleId: string; memoryType: string; summary: string; extra: Record<string, unknown>; status: string;
  happenedAt: string | null; createdAt: string; updatedAt: string; reinforcement?: number; emotionalWeight?: number; hasEmbedding?: boolean;
  sourceRef: MemoryOrigin & { sourceKeys: string[]; sources: MemoryOrigin[] };
};
export const typeLabels: Record<string, string> = { fact: '事实', profile: '资料', preference: '偏好', procedure: '习惯与要求', event: '共同经历' };
export const statusLabels: Record<string, string> = { active: '有效', rejected: '已拒绝', forgotten: '已忘记', superseded: '已替代' };
export const sourceLabels: Record<string, string> = { message: '消息来源', turn: '对话回合', consolidation: '整理窗口', manual: '手动记录' };
export const formatDate = (value: string | null, short = false) => {
  if (!value || Number.isNaN(new Date(value).getTime())) return '未记录';
  return short ? new Date(value).toLocaleDateString() : new Date(value).toLocaleString();
};
export const originsOf = (memory: Memory): MemoryOrigin[] => memory.sourceRef?.sources?.length ? memory.sourceRef.sources : memory.sourceRef ? [memory.sourceRef] : [];
