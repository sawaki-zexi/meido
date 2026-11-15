import type { CollectionResult } from '../../api/materials';

export function collectionFeedback(result: CollectionResult, fallback: string) {
  if (result.descriptionOutcome === 'proposal_retained') return '原说明未更改；本次新说明已另存为建议，暂不用于聊天。';
  if (result.descriptionOutcome === 'filled') return '已补充当前来源的说明，原图没有重复保存。';
  if (result.deduplicated && result.effectiveSemanticStatus === 'pending') return '已保留这次收藏来源；该来源尚无说明，不自动推荐。';
  return fallback;
}
