import type { ChannelAttachment } from "./channelMessagePresentation";
import { AttachmentImage } from "../../ui/AttachmentImage";
import { SaveMaterialAction } from '../materials/SaveMaterialAction';

export function ChannelAttachments({ roleId, messageId, attachments, onMediaLoad, onOpenImage }: { roleId: string; messageId?: string; attachments: ChannelAttachment[]; onMediaLoad?: () => void; onOpenImage?: (href: string, name: string) => void }) {
  return <>{attachments.map((attachment) => {
    const href = `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachment.id)}`;
    return attachment.kind === "image"
      ? <div key={attachment.id}><AttachmentImage href={href} src={href} alt={attachment.name} imageClassName="channel-attachment-image" onMediaLoad={onMediaLoad} onOpen={onOpenImage ? () => onOpenImage(href, attachment.name) : undefined} />{messageId && <SaveMaterialAction key={`${roleId}:${messageId}:${attachment.id}`} roleId={roleId} messageId={messageId} attachmentId={attachment.id} />}</div>
      : <a className="channel-attachment-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
  })}</>;
}
