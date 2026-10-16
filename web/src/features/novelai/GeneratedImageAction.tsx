import { useEffect, useState } from 'react';
import { novelai, type GenerationSelection } from '../../api/novelai';
import type { ChatAttachment } from '../../api/types';

/** Message metadata identifies candidates; only the host can verify the actual image and original slot. */
export function GeneratedImageAction({ roleId, messageId, image, attemptIds, onSelect }: {
  roleId: string; messageId: string; image: ChatAttachment; attemptIds: string[];
  onSelect: (selection: GenerationSelection) => void;
}) {
  const [selection, setSelection] = useState<GenerationSelection | null>(null);
  const attemptsKey = JSON.stringify(attemptIds);
  useEffect(() => {
    const controller = new AbortController();
    setSelection(null);
    void novelai.forAttachment(roleId, image.id, messageId, controller.signal).then(({ generation }) => {
      if (controller.signal.aborted || !generation.parameters || generation.roleId !== roleId || generation.deliveryMessageId !== messageId
        || !attemptIds.includes(generation.attemptId)) return;
      const owned = generation.attachments.find((attachment) => attachment.id === image.id && attachment.kind === 'image');
      if (owned) setSelection({ generation, image: owned });
    }).catch(() => { /* Ordinary attachments and unavailable source messages have no regeneration action. */ });
    return () => controller.abort();
  }, [roleId, messageId, image.id, attemptsKey]);
  return selection && <button type="button" onClick={() => onSelect(selection)}>重新生成 {image.name}</button>;
}
