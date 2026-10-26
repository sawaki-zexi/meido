import { MessageMarkdown } from "../../ui/MessageMarkdown";

type Part = { status: string; content?: string; fileName?: string; platformMessageId?: string; confirmedContent?: string; withoutReply?: boolean; plainText?: boolean; error?: string };

export function deliveryLabel(metadata: Record<string, unknown>): string | null {
  if (metadata.partialDelivery) return metadata.deliveryStatus === "uncertain" ? "部分已发送 · 其余结果不确定" : "部分已发送";
  if (metadata.receiptKind === "transport_acceptance" && metadata.deliveryStatus === "sent") return "已提交 Bot";
  return ({ pending: "发送中", sent: "已发送", failed: "发送失败", uncertain: "发送结果不确定" } as Record<string, string>)[String(metadata.deliveryStatus)] ?? null;
}

export function DeliveryDetails({ metadata }: { metadata: Record<string, unknown> }) {
  const parts = Array.isArray(metadata.parts) ? metadata.parts.filter((part): part is Part => part && typeof part === "object" && typeof part.status === "string") : [];
  if (!parts.length) return null;
  const pending = metadata.deliveryStatus === "pending";
  const statuses: Record<string, string> = { sent: "已发送", failed: "发送失败", uncertain: "结果不确定", sending: "等待确认", pending: pending ? "等待发送" : "未发送" };
  return <details className="channel-delivery-parts"><summary>分段送达详情</summary>
    <ol>{parts.map((part, index) => <li key={index}>
      <strong>第 {index + 1} 段 · {statuses[part.status] ?? "结果不确定"}</strong>
      {part.content && <MessageMarkdown content={part.content} />}
      {part.fileName && <p>附件：{part.fileName}</p>}
      {part.confirmedContent && part.status !== "sent" && <p>此前已确认：{part.confirmedContent}</p>}
      {part.platformMessageId && <small>消息编号：{part.platformMessageId}</small>}
      {(part.withoutReply || part.plainText) && <p>{part.withoutReply ? "原引用已失效，已在原聊天取消引用。" : ""}{part.plainText ? "已改为普通文字。" : ""}</p>}
      {part.error === "delivery_deadline" && <p>等待已到期，已停止发送。</p>}
      {part.error === "source_revoked" && <p>原发送许可已失效，已停止发送。</p>}
      {part.error === "delivery_queue_full" && <p>待发送消息已满，本条未发送。</p>}
    </li>)}</ol>
  </details>;
}
