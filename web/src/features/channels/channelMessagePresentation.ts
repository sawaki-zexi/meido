import type { Role } from "../../api/types";
import type { Account } from "./channelAccounts";

export type ViaAccount = { platform?: string; platformAccountId?: string; accountId?: string; displayName?: string; avatarUrl?: string | null };
export type ConversationMessage = { id: string; sequence: number; role: string; content: string; status?: string; createdAt: string; metadata: Record<string, unknown> };
export type ChannelAttachment = { id: string; name: string; kind?: string };

function attachments(value: unknown): ChannelAttachment[] {
  return Array.isArray(value) ? value.filter((item): item is ChannelAttachment => Boolean(
    item && typeof item === "object" && typeof item.id === "string" && typeof item.name === "string",
  )) : [];
}

export function messagePresentation(message: ConversationMessage, messages: ConversationMessage[], role: Role, account: Account | null) {
  const source = message.metadata.viaAccount as ViaAccount | undefined;
  const replyTo = typeof message.metadata.replyTo === "string" && message.metadata.replyTo ? message.metadata.replyTo : null;
  const replyTarget = replyTo ? messages.find((item) => String(item.metadata.platformMessageId ?? "") === replyTo) : undefined;
  const replySender = replyTarget ? String(replyTarget.metadata.senderName ?? (replyTarget.role === "assistant" ? role.name : "访客")) : String(message.metadata.replyToSenderName || message.metadata.replyToSenderId || "");
  const quotedText = typeof message.metadata.replyToContent === "string" ? message.metadata.replyToContent : "";
  const mentionedIds = Array.isArray(message.metadata.mentionedIds) ? message.metadata.mentionedIds.map(String) : [];
  const mentionedNames = mentionedIds.map((id) => {
    if (id === account?.platformId || id === source?.platformAccountId) return account?.displayName || source?.displayName || role.name;
    const target = messages.find((item) => String(item.metadata.senderId ?? "") === id);
    return target ? String(target.metadata.senderName ?? id) : id;
  });
  const senderName = message.role === "assistant" ? role.name : String(message.metadata.senderName || message.metadata.senderId || "访客");
  // Delivery failures also set status=failed; runtimeReason preserves a separate
  // generation failure when a partial reply was sent to the platform.
  const hasDeliveryStatus = ["pending", "sent", "failed", "uncertain"].includes(String(message.metadata.deliveryStatus));
  return {
    source, replyTo, replyTarget, replySender, quotedText, mentionedNames, senderName,
    attachments: attachments(message.metadata.attachments), quotedAttachments: attachments(message.metadata.quotedAttachments),
    avatarUrl: message.role === "assistant" ? role.avatarUrl : typeof message.metadata.senderAvatarUrl === "string" ? message.metadata.senderAvatarUrl : null,
    failed: message.status === "failed" && (message.metadata.runtimeReason === "failed" || !hasDeliveryStatus),
    error: typeof message.metadata.error === "string" ? message.metadata.error : "",
  };
}
export type MessagePresentation = ReturnType<typeof messagePresentation>;

function date(value: string): Date | null {
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

export function shortTime(value: string): string {
  const parsed = date(value);
  return parsed ? parsed.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "";
}

export function dayLabel(value: string): string {
  const parsed = date(value);
  if (!parsed) return "";
  const now = new Date();
  const midnight = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(parsed)) / 86_400_000);
  if (days <= 0) return "";
  if (days === 1) return "昨天";
  if (days < 7) return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][parsed.getDay()];
  return `${parsed.getFullYear() === now.getFullYear() ? "" : `${parsed.getFullYear()}/`}${parsed.getMonth() + 1}/${parsed.getDate()}`;
}

export function messageTimeSeparators(messages: ConversationMessage[]): string[] {
  let previous: number | null = null;
  return messages.map((message) => {
    const parsed = date(message.createdAt);
    if (!parsed) return "";
    const time = parsed.getTime();
    const separated = previous === null || time - previous > 5 * 60_000;
    previous = time;
    if (!separated) return "";
    return [dayLabel(message.createdAt), shortTime(message.createdAt)].filter(Boolean).join(" ");
  });
}

export function conversationTime(value: string): string {
  const parsed = date(value);
  if (!parsed) return "";
  return dayLabel(value) || shortTime(value);
}

// Markdown preview formatting adapted from Shiori-Agent, Copyright (c) 2026
// YinFengWindy (MIT). See docs/reference/third-party-notices.md.
export function conversationPreview(message: { content: string; role: string; status?: string }): string {
  const text = message.content
    .replace(/```[\s\S]*?(?:```|$)/g, " [代码] ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/gm, "")
    .replace(/^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/gm, "")
    .replace(/\|/g, " ")
    .replace(/(\*\*|__|~~)(.*?)\1/g, "$2")
    .replace(/(^|[^\w*])[*_]([^*_\n]+)[*_](?=[^\w*]|$)/g, "$1$2")
    .replace(/\s+/g, " ").trim();
  // The list projection has no metadata to distinguish generation and delivery.
  if (message.status === "failed") return text ? `回复需关注 · ${text}` : "回复需关注";
  if (text) return text;
  return message.status === "streaming" ? "正在回复…" : "[非文本消息]";
}
