// Adapted from Shiori-Agent d149de7 ChatTypingSparkles and icons/brand.tsx.
// Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
export function ChatTypingSparkles() {
  return <span className="chat-typing-sparkles" aria-hidden="true">
    {[0, 1, 2].map(index => <span key={index}><svg viewBox="4 4 16 16">
      <path d="M12 4.2 Q13.1 10.4 19.3 12 Q13.1 13.6 12 19.8 Q10.9 13.6 4.7 12 Q10.9 10.4 12 4.2 Z" fill="currentColor" />
    </svg></span>)}
  </span>;
}
