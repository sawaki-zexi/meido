import type { GenerationView } from '../../api/novelai';

export function generationErrorMessage(generation: GenerationView): string {
  return generation.errorMessage || errorLabels[generation.errorCode] || (generation.errorCode
    ? '本次请求未正常完成，请检查当前设置与归属历史。' : '这条记录暂时没有可预览图片。');
}

export const statusLabels: Record<string, string> = {
  completed_without_image: '观察完成，未生成图片', prepared: '等待生成', dispatching: '生成中', received: '已收图，等待保存',
  succeeded: '已生成', unknown: '消费结果不确定', failed_response: '远端返回错误，可能已计费',
  local_pending: '本地保存待恢复', cancelled_before_dispatch: '出网前已取消', rejected: '未准入',
};
export const deliveryLabels: Record<string, string> = {
  sent: '已交付原来源', uncertain: '交付结果不确定，不会再次发送', failed: '交付失败，图片保留在历史',
  suppressed: '已取消展示，图片保留在历史', history_only: '仅保存在历史', pending: '尚未交付', sending: '正在交付原来源',
};
export const errorLabels: Record<string, string> = {
  response_not_verifiable: '无法完整验证远端图片，已保留可验证的图片。',
  replacement_conflict: '已生成，原消息已改变；新图仅保留在历史，未替换其他附件。',
  unexpected_multiple_images: '返回图片张数与请求不一致，已保留收到的图片，请在历史查看。',
  local_media_pending: '图片本地保存尚未完成，只恢复本地步骤。',
  transport_unknown: '网络结果无法确认，原请求不会再次发送。',
  scene_none: '当前没有需要生成图片的场景。', scene_closed: '场景已结束，本次不生成图片。',
  scene_cooldown: '当前场景仍在五个主人回合的冷却期内。', scene_visual_duplicate: '此画面已经处理，本次不重复生成。',
  scene_observation_failed: '场景观察未完成；辅助请求可能已经计费，不会自动重试。',
};
export const writebackLabels: Record<string, string> = {
  writing: '正在写回或核对本地卡片图。', uncertain: '卡片图写回结果尚未确认，生成图已保留历史。',
  applied: '已更新本角色卡片图。', failed: '卡片图写回失败，生成图已保留历史；不会重新生成。',
};
