import { useState } from "react";
import { Dialog } from "../../ui/Dialog";
import { AttachmentImage } from "../../ui/AttachmentImage";
import { MessageMarkdown } from "../../ui/MessageMarkdown";
import { ChannelAttachments } from "./ChannelAttachments";
import type { ConversationMessage, MessagePresentation } from "./channelMessagePresentation";
import { DeliveryDetails } from "./DeliveryDetails";

export function PhoneMessage({ message, view, roleId, timeLabel, delivery, source, onMediaLoad }: {
  message: ConversationMessage;
  view: MessagePresentation;
  roleId: string;
  timeLabel: string;
  delivery: string | null;
  source: string | null;
  onMediaLoad: () => void;
}) {
  const outgoing = message.role === "assistant";
  const [quoteExpanded, setQuoteExpanded] = useState(false);
  const [image, setImage] = useState<{ href: string; name: string } | null>(null);
  const openImage = (href: string, name: string) => setImage({ href, name });
  const hasBody = Boolean(message.content || view.attachments.length || view.quotedAttachments.length || view.replyTo || view.mentionedNames.length);
  const quotedText = view.quotedText || view.replyTarget?.content || (view.quotedAttachments.length ? "附件" : "原文未加载");
  const longQuote = quotedText.length > 40 || quotedText.split("\n").length > 2;
  return <>
    {timeLabel && <div className="phone-message-date"><time dateTime={message.createdAt}>{timeLabel}</time></div>}
    <div className={`phone-message-row${outgoing ? " outgoing" : ""}`}>
      <span className="phone-message-avatar" title={view.senderName} aria-hidden="true">{view.avatarUrl ? <img src={view.avatarUrl} alt="" /> : view.senderName.slice(0, 1)}</span>
      <div className="phone-message-main">
        {!outgoing && <span className="phone-message-name">{message.metadata.listening === true && <span className="phone-message-listened">旁听</span>}{view.senderName}{message.metadata.senderIsOwner === true && <span className="phone-message-owner">这是我</span>}</span>}
        {view.replyTo && <blockquote className="phone-message-quote"><strong>{view.replySender || "引用消息"}</strong><span className={longQuote && !quoteExpanded ? "is-collapsed" : ""}>{quotedText}</span>{longQuote && <button type="button" onClick={() => setQuoteExpanded((expanded) => !expanded)}>{quoteExpanded ? "收起引用" : "展开引用"}</button>}</blockquote>}
        {view.quotedAttachments.length > 0 && <div className="phone-message-quote" role="group" aria-label="引用消息附件"><ChannelAttachments roleId={roleId} attachments={view.quotedAttachments} onMediaLoad={onMediaLoad} onOpenImage={openImage} /></div>}
        {(message.content || view.mentionedNames.length > 0 || view.failed || !hasBody) && <div className={`phone-message-bubble${view.failed ? " is-failed" : ""}${message.metadata.listening === true ? " is-listened" : ""}`}>
          {view.mentionedNames.length > 0 && <p>{view.mentionedNames.map((name, index) => <span className="phone-message-mention" key={`${name}:${index}`}>@{name} </span>)}</p>}
          {message.content && <MessageMarkdown content={message.content} />}
          {view.failed && (!message.content || view.error !== message.content) && <p className="phone-message-error">{view.error || "回复未能完成"}</p>}
          {!hasBody && !view.failed && <p className="phone-message-placeholder">{message.status === "streaming" ? "正在回复…" : "[非文本消息]"}</p>}
        </div>}
        {view.attachments.length > 0 && <div className="phone-message-media"><ChannelAttachments roleId={roleId} attachments={view.attachments} onMediaLoad={onMediaLoad} onOpenImage={openImage} /></div>}
        <DeliveryDetails metadata={message.metadata} />
        <div className="phone-message-meta">
          {view.failed && <span className="phone-message-failure">回复失败</span>}
          {delivery && <span className={`phone-message-delivery ${message.metadata.deliveryStatus}`}>{delivery}</span>}
          <details className="phone-message-details"><summary aria-label={`消息详情 · ${view.senderName}`} title="消息详情">···</summary><div>
            <p>{view.senderName}{message.metadata.senderIsOwner === true ? " · 主人" : ""}</p>
            {source && <p>{source}</p>}
            <p>{new Date(message.createdAt).toLocaleString()}</p>
            {Boolean(message.metadata.platformMessageId) && <p>消息编号：{String(message.metadata.platformMessageId)}</p>}
            {view.replyTo && <p>引用消息：{view.replyTo}</p>}
          </div></details>
        </div>
      </div>
    </div>
    {image && <Dialog title="图片预览" onClose={() => setImage(null)} modalFocus><AttachmentImage src={image.href} alt={image.name} imageClassName="phone-image-preview" preview /></Dialog>}
  </>;
}
