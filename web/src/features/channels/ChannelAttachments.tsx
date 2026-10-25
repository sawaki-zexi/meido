import type { ChannelAttachment } from "./channelMessagePresentation";
import { AttachmentImage } from "../../ui/AttachmentImage";

export function ChannelAttachments({ roleId, attachments, onMediaLoad, onOpenImage }: { roleId: string; attachments: ChannelAttachment[]; onMediaLoad?: () => void; onOpenImage?: (href: string, name: string) => void }) {
  return <>{attachments.map((attachment) => {
    const href = `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachment.id)}`;
    return attachment.kind === "image"
      ? <AttachmentImage key={attachment.id} href={href} src={href} alt={attachment.name} imageClassName="channel-attachment-image" onMediaLoad={onMediaLoad} onOpen={onOpenImage ? () => onOpenImage(href, attachment.name) : undefined} />
      : <a className="channel-attachment-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
  })}</>;
}
