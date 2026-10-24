export type Preparation = {
  id: string; mailId?: string; decision: "draft" | "no_reply" | "needs_input"; confidence?: "low" | "medium" | "high" | "unknown"; reason: string; questions: string[];
  createdAt: string; provider?: string; model: string; modelConfigurationId?: string; modelUsage?: { inputTokens?: number; outputTokens?: number; cacheReadTokens?: number; cacheWriteTokens?: number; reasoningTokens?: number; cost?: { input?: number; output?: number; cacheRead?: number; cacheWrite?: number; total?: number } }; promptHash?: string; pipelineVersion?: number; stale: boolean; warnings: string[];
  contextMetadata?: { currentEmail?: { injected: boolean; sourceChars: number; providedChars: number; truncated: boolean }; thread?: { availableCount: number; injectedCount: number; maxMessages: number }; sentHistory?: { retrievalFailed?: boolean; retrievedCount: number; injectedCount: number; maxRetrieved: number; maxExamples: number }; ownerKnowledge?: { retrievalFailed?: boolean; availableCount: number; injectedCount: number; maxResults: number }; writingStyle?: { custom: boolean }; prompt?: { hash?: string; messageCount?: number; inputChars?: number }; warnings?: { count: number } };
  sources: { kind: string; id: string; title: string; location?: string; url?: string; sender?: string; receivedAt?: string; sourceChars?: number; providedChars?: number; truncated?: boolean; quotedRemoved?: boolean; accountScope?: string; sourceUid?: string; mailbox?: string; inReplyTo?: string; references?: string }[];
};

function safeLink(value?: string) {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function formatSourceDate(value?: string) {
  if (!value) return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatUsage(value: Preparation["modelUsage"]) {
  if (!value) return "";
  const parts: string[] = [];
  if (typeof value.inputTokens === "number") parts.push(`输入 ${value.inputTokens} tokens`);
  if (typeof value.outputTokens === "number") parts.push(`输出 ${value.outputTokens} tokens`);
  if (typeof value.cost?.total === "number") parts.push(`费用 ${value.cost.total}`);
  return parts.join(" · ");
}

export function MailPreparation({ value, onSelect }: { value: Preparation; onSelect: (id: string) => void }) {
  return <section className="mail-preparation" aria-label="邮件准备结果">
    {value.reason && <p>{value.reason}</p>}
    {value.confidence && value.confidence !== "unknown" && <p className="muted">模型自评信心：{value.confidence === "high" ? "高" : value.confidence === "medium" ? "中" : "低"}。此评估不替代主人确认。</p>}
    {value.stale && <p className="mail-item-error">往来已有更新，请重新判断后再补充。</p>}
    {value.warnings.length > 0 && <ul className="mail-errors">{value.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
    <details open><summary>提供给模型的资料 · {value.sources.length} 项</summary>
      <p className="muted">记录本次提供的资料，原文可能随后变化。</p>
      <ul className="mail-sources">{value.sources.map((source, index) => {
        const href = safeLink(source.url);
        return <li key={`${source.kind}:${source.id}:${index}`}>
          {source.kind === "thread" || source.kind === "sent_sample"
            ? <button type="button" onClick={() => onSelect(source.id)}>{source.title || "（无主题邮件）"}</button>
            : href ? <a href={href} target="_blank" rel="noreferrer">{source.title}</a> : <span>{source.title}</span>}
          <small>{source.kind === "owner_knowledge" ? "主人资料" : source.kind === "sent_sample" ? "历史回复" : source.kind === "current_email" ? "当前来信" : "当前往来"}{source.receivedAt ? ` · ${formatSourceDate(source.receivedAt)}` : ""}{source.location ? ` · ${source.location}` : ""}{source.sender ? ` · ${source.sender}` : ""}{source.truncated ? ` · 已提供 ${source.providedChars ?? 0}/${source.sourceChars ?? 0} 字符` : ""}{source.quotedRemoved ? " · 已去除引用" : ""}</small>
        </li>;
      })}</ul>
    </details>
    <small className="muted">{new Date(value.createdAt).toLocaleString()}{value.provider || value.model ? ` · ${[value.provider, value.model].filter(Boolean).join("/")}` : ""}{value.modelConfigurationId ? ` · 配置 ${value.modelConfigurationId}` : ""}{value.pipelineVersion ? ` · 流水线 v${value.pipelineVersion}` : ""}{value.promptHash ? ` · 输入 ${value.promptHash.slice(0, 12)}` : ""}{formatUsage(value.modelUsage) ? ` · ${formatUsage(value.modelUsage)}` : ""}</small>
    {value.contextMetadata && <small className="muted">线程 {value.contextMetadata.thread?.injectedCount ?? 0} 封 · 历史回复 {value.contextMetadata.sentHistory?.injectedCount ?? 0} 封{value.contextMetadata.sentHistory?.retrievalFailed ? "（读取失败）" : ""} · 主人资料 {value.contextMetadata.ownerKnowledge?.injectedCount ?? 0} 条{value.contextMetadata.ownerKnowledge?.retrievalFailed ? "（读取失败）" : ""}{value.contextMetadata.writingStyle?.custom ? " · 使用主人写作偏好" : ""}</small>}
  </section>;
}
