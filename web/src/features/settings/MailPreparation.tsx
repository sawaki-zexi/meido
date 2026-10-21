export type Preparation = {
  id: string; decision: "draft" | "no_reply" | "needs_input"; reason: string; questions: string[];
  createdAt: string; model: string; stale: boolean; warnings: string[];
  sources: { kind: string; id: string; title: string; location?: string; url?: string; sender?: string; receivedAt?: string }[];
};

function safeLink(value?: string) {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

export function MailPreparation({ value, onSelect }: { value: Preparation; onSelect: (id: string) => void }) {
  return <section className="mail-preparation" aria-label="邮件准备结果">
    {value.reason && <p>{value.reason}</p>}
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
          <small>{source.kind === "owner_knowledge" ? "主人资料" : source.kind === "sent_sample" ? "历史回复" : "当前往来"}{source.location ? ` · ${source.location}` : ""}{source.sender ? ` · ${source.sender}` : ""}</small>
        </li>;
      })}</ul>
    </details>
    <small className="muted">{new Date(value.createdAt).toLocaleString()}{value.model ? ` · ${value.model}` : ""}</small>
  </section>;
}
