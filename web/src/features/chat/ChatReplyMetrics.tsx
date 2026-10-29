// Shiori-Agent ChatReplyMetrics layout; native Meido metrics. MIT attribution in third_party/shiori-agent.
export function ChatReplyMetrics({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object') return null;
  const metrics = value as Record<string, unknown>;
  const duration = metrics.thinkingDurationMs;
  const tokens = metrics.totalTokens;
  const hasDuration = typeof duration === 'number' && Number.isFinite(duration) && duration >= 0;
  const hasTokens = typeof tokens === 'number' && Number.isFinite(tokens) && tokens >= 0;
  if (!hasDuration && !hasTokens) return null;
  return <div className="chat-reply-metrics">
    {hasDuration && <span title="从回合开始到首段正文返回的时间；无流式正文时计至模型回复完成">思考 {(duration / 1000).toFixed(1)} 秒</span>}
    {hasTokens && <span title="本回合各次模型请求实际报告的输入与输出 token 总量">{tokens.toLocaleString('en-US')} tokens</span>}
  </div>;
}
