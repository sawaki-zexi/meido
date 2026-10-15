import { useCallback, useEffect, useRef, useState } from "react";

type Props = { file: File; onCancel: () => void; onAccept: (file: File, original: File) => void; onError: (message: string) => void; shape?: "square" | "circle" | "card" };
const SIZE = 512;

/** Crop in output coordinates; project those coordinates into the responsive preview. */
export function AvatarCropper({ file, onCancel, onAccept, onError, shape = "square" }: Props) {
  const height = shape === "card" ? SIZE * 5 / 4 : SIZE;
  const [source, setSource] = useState<string | null>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const imageRef = useRef<HTMLImageElement>(null);
  const cropperRef = useRef<HTMLElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number } | null>(null);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    setDimensions({ width: 0, height: 0 });
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    dragRef.current = null;
    setDragging(false);
    setSource(url);
    return () => URL.revokeObjectURL(url);
  }, [file, shape]);

  // The image must cover the output frame; the overflow on each axis is draggable.
  const fit = dimensions.width && dimensions.height ? Math.max(SIZE / dimensions.width, height / dimensions.height) : 1;
  const scale = fit * zoom;
  const bounds = () => {
    const image = imageRef.current;
    if (!image) return { x: 0, y: 0 };
    return { x: Math.max(0, (image.naturalWidth * scale - SIZE) / 2), y: Math.max(0, (image.naturalHeight * scale - height) / 2) };
  };
  const changeZoom = useCallback((nextZoom: number) => {
    setZoom(nextZoom);
    const nextScale = fit * nextZoom;
    const maxX = Math.max(0, (dimensions.width * nextScale - SIZE) / 2);
    const maxY = Math.max(0, (dimensions.height * nextScale - height) / 2);
    setOffset((current) => ({
      x: Math.max(-maxX, Math.min(maxX, current.x)),
      y: Math.max(-maxY, Math.min(maxY, current.y)),
    }));
  }, [dimensions, fit, height]);

  useEffect(() => {
    if (dimensions.width) cropperRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [dimensions]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !dimensions.width) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      changeZoom(Math.max(1, Math.min(3, zoom * (event.deltaY > 0 ? 0.92 : 1.08))));
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, [changeZoom, dimensions.width, zoom]);
  const finish = () => {
    const image = imageRef.current;
    if (!image || !dimensions.width || !dimensions.height) return;
    const canvas = document.createElement("canvas");
    canvas.width = SIZE;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) { onError("无法处理这张图片，请换一张图片再试"); return; }
    const cropX = (image.naturalWidth * scale - SIZE) / 2 - offset.x;
    const cropY = (image.naturalHeight * scale - height) / 2 - offset.y;
    context.drawImage(image, cropX / scale, cropY / scale, SIZE / scale, height / scale, 0, 0, SIZE, height);
    canvas.toBlob((blob) => {
      if (blob) onAccept(new File([blob], file.name.replace(/\.[^.]+$/, "") + ".png", { type: "image/png" }), file);
      else onError("无法生成裁切后的图片，请重试");
    }, "image/png");
  };

  return <section ref={cropperRef} className="avatar-cropper" aria-label={shape === "card" ? "调整卡片裁切" : "调整头像裁切"}>
    <div
      ref={viewportRef}
      className={`avatar-crop-window crop-${shape}${dragging ? " is-dragging" : ""}`}
      onPointerDown={(event) => { if (event.button !== 0) return; dragRef.current = { x: event.clientX, y: event.clientY, offsetX: offset.x, offsetY: offset.y }; event.currentTarget.setPointerCapture(event.pointerId); setDragging(true); }}
      onPointerMove={(event) => {
        if (!dragRef.current) return;
        const boundsNow = bounds();
        const rect = event.currentTarget.getBoundingClientRect();
        const dx = (event.clientX - dragRef.current.x) * SIZE / rect.width;
        const dy = (event.clientY - dragRef.current.y) * height / rect.height;
        setOffset({ x: Math.max(-boundsNow.x, Math.min(boundsNow.x, dragRef.current.offsetX + dx)), y: Math.max(-boundsNow.y, Math.min(boundsNow.y, dragRef.current.offsetY + dy)) });
      }}
      onPointerUp={() => { dragRef.current = null; setDragging(false); }}
      onPointerCancel={() => { dragRef.current = null; setDragging(false); }}
    >
      {source && <img
        ref={imageRef}
        src={source}
        alt={shape === "card" ? "卡片裁切预览" : "头像裁切预览"}
        draggable={false}
        onError={() => { setDimensions({ width: 0, height: 0 }); onError("无法读取这张图片，请换一张图片再试"); }}
        onLoad={(event) => { setDimensions({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight }); setOffset({ x: 0, y: 0 }); }}
        style={{
          width: dimensions.width ? `${dimensions.width * scale / SIZE * 100}%` : undefined,
          height: dimensions.height ? `${dimensions.height * scale / height * 100}%` : undefined,
          left: `${50 + offset.x / SIZE * 100}%`,
          top: `${50 + offset.y / height * 100}%`,
          transform: "translate(-50%, -50%)",
        }}
      />}
    </div>
    <label className="avatar-crop-zoom">缩放<input aria-label="缩放" type="range" min="1" max="3" step="0.01" value={zoom} disabled={!dimensions.width} onChange={(event) => changeZoom(Number(event.target.value))} /></label>
    <div className="avatar-crop-actions">
      <button type="button" className="ghost" onClick={onCancel}>取消</button>
      <button type="button" className="primary" disabled={!dimensions.width} onClick={finish}>保存</button>
    </div>
  </section>;
}
