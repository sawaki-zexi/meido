import { useCallback, useLayoutEffect, useRef } from "react";
import type { Message } from "../../api/types";

type Props = { roleId: string; messages: Message[]; sending: boolean; highlightedId: string | null };

/** Follow new content until the reader scrolls up; sending resumes following. */
export function useChatScroll({ roleId, messages, sending, highlightedId }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const following = useRef(!highlightedId);
  const previous = useRef({ roleId, sending, highlightedId });
  const size = useRef({ content: 0, viewport: 0 });
  const scrollToBottom = useCallback(() => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const content = viewport.scrollHeight;
    const visible = viewport.clientHeight;
    if (following.current) {
      // Restarting a smooth animation for every token can prevent any progress.
      viewport.scrollTop = Math.max(0, content - visible);
    }
    size.current = { content, viewport: visible };
  }, []);

  useLayoutEffect(() => {
    const last = previous.current;
    if (last.roleId !== roleId || last.highlightedId !== highlightedId) following.current = !highlightedId;
    if (sending && !last.sending) following.current = true;
    previous.current = { roleId, sending, highlightedId };
    scrollToBottom();
  }, [roleId, messages, sending, highlightedId, scrollToBottom]);

  const hasMessages = messages.length > 0;
  useLayoutEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(scrollToBottom);
    if (scrollRef.current) observer.observe(scrollRef.current);
    if (listRef.current) observer.observe(listRef.current);
    return () => observer.disconnect();
  }, [roleId, hasMessages, scrollToBottom]);

  const onScroll = () => {
    const viewport = scrollRef.current;
    if (!viewport) return;
    const nearBottom = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 64;
    const resized = viewport.scrollHeight !== size.current.content || viewport.clientHeight !== size.current.viewport;
    // Reflow can emit scroll before ResizeObserver. It is not a reading gesture.
    if (!resized || nearBottom) following.current = nearBottom;
  };

  return { scrollRef, listRef, onScroll };
}
