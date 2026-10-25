import { useLayoutEffect, useRef, useState } from "react";
import "./attachment-image.css";

type Props = {
  src: string;
  alt: string;
  href?: string;
  imageClassName?: string;
  preview?: boolean;
  onOpen?: () => void;
  onMediaLoad?: () => void;
};

/** Keep the original image URL (including animated GIFs); only retry its browser load. */
export function AttachmentImage(props: Props) {
  // A different role/source must never inherit a previous image's failure or success.
  return <ImageLoad key={props.src} {...props} />;
}

function ImageLoad({ src, alt, href, imageClassName, preview, onOpen, onMediaLoad }: Props) {
  const [status, setStatus] = useState<"loading" | "loaded" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const container = useRef<HTMLSpanElement>(null);
  const mediaLoad = useRef(onMediaLoad);
  mediaLoad.current = onMediaLoad;
  // Notify after the placeholder is replaced, so chat scroll measures the new layout.
  useLayoutEffect(() => {
    if (status !== "loading") mediaLoad.current?.();
  }, [status]);

  const image = <img key={attempt} className={imageClassName} src={src} alt={alt} onLoad={() => {
    if (status === "loaded") onMediaLoad?.();
    else setStatus("loaded");
  }} onError={() => setStatus("failed")} />;

  return <span ref={container} tabIndex={-1} className={`attachment-image is-${status}${preview ? " attachment-image--preview" : ""}`}>
    {href ? <a className="attachment-image-link" href={href} target="_blank" rel="noreferrer" aria-label={alt} onClick={onOpen ? event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault(); onOpen();
    } : undefined}>{image}</a> : image}
    {status !== "loaded" && <span className="attachment-image-feedback">
      <span role="status">{status === "loading" ? "图片加载中…" : "图片加载失败"}</span>
      {status === "failed" && <button type="button" className="attachment-image-retry" aria-label={`重新加载图片 ${alt}`} onClick={() => {
        container.current?.focus();
        setStatus("loading");
        setAttempt(current => current + 1);
      }}>重试</button>}
    </span>}
  </span>;
}
