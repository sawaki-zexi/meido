// Adapted from Shiori-Agent d149de7, ChatThinkingBlock.tsx (MIT; see third_party/shiori-agent).
import { useId, useState } from 'react';
import { CaretDown } from '@phosphor-icons/react/dist/csr/CaretDown';
import { Sparkle } from '@phosphor-icons/react/dist/csr/Sparkle';

export function ChatThinkingBlock({ content, streaming }: { content: string; streaming: boolean }) {
  const [expanded, setExpanded] = useState(true);
  const id = useId();
  if (!content) return null;
  return <div className="chat-thinking-block">
    <button type="button" className="chat-thinking-toggle" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(value => !value)}>
      <Sparkle size={15} weight="fill" aria-hidden />
      <span className={`chat-thinking-label${streaming ? ' chat-thinking-label-streaming' : ''}`}>思考中</span>
      <CaretDown size={14} className={expanded ? 'is-expanded' : ''} aria-hidden />
    </button>
    <div id={id} className={`chat-thinking-content${expanded ? ' is-expanded' : ''}`} aria-hidden={!expanded}>
      <div><div className="chat-thinking-text">{content}{streaming && <span className="chat-thinking-cursor" aria-hidden />}</div></div>
    </div>
  </div>;
}
