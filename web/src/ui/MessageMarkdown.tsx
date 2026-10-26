import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkGfm from "remark-gfm";
import "./message-markdown.css";

const remarkPlugins = [remarkGfm, remarkCjkFriendly, remarkBreaks];

function externalLink(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

const components: Components = {
  a({ href, children, title }) {
    const safeHref = externalLink(href);
    return safeHref ? <a href={safeHref} title={title} target="_blank" rel="noopener noreferrer">{children}</a> : <>{children}</>;
  },
  // Media is displayed through the existing role-scoped attachment components.
  img: ({ alt }) => alt ? <span>{alt}</span> : null,
  table: ({ children }) => <div className="message-markdown-table" tabIndex={0}><table>{children}</table></div>,
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
};

/** Display message source without changing the stored transcript. */
export const MessageMarkdown = memo(function MessageMarkdown({ content }: { content: string }) {
  return <div className="message-markdown"><Markdown remarkPlugins={remarkPlugins} components={components}>{content}</Markdown></div>;
});
