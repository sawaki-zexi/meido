import type { ChannelAttachment } from "./channelMessagePresentation";

export function ChannelAttachments({ roleId, attachments, onMediaLoad, onOpenImage }: { roleId: string; attachments: ChannelAttachment[]; onMediaLoad?: () => void; onOpenImage?: (href: string, name: string) => void }) {
  return <>{attachments.map((attachment) => {
    const href = `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachment.id)}`;
    return attachment.kind === "image"
      ? <a href={href} target="_blank" rel="noreferrer" key={attachment.id} onClick={onOpenImage ? (event) => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); onOpenImage(href, attachment.name);
      } : undefined}><img className="channel-attachment-image" src={href} alt={attachment.name} onLoad={onMediaLoad} /></a>
      : <a className="channel-attachment-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
  })}</>;
}
