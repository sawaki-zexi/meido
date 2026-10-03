import { useEffect, useRef, useState } from "react";

type Props = { file: File; onCancel: () => void; onAccept: (file: File, original: File) => void; onError: (message: string) => void; shape?: "square" | "circle" };
const SIZE = 512;

/** Square avatar crop with pointer positioning and a bounded zoom control. */
export function AvatarCropper({ file, onCancel, onAccept, onError, shape = "square" }: Props) {
  const [source, setSource] = useState<string | null>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const imageRef = useRef<HTMLImageElement>(null);
  const dragRef = useRef<{ x: number; y: number; offsetX: number; offsetY: number } | null>(null);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    setSource(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  // The image must cover the square viewport; the overflow on each axis is draggable.
  const fit = dimensions.width && dimensions.height ? Math.max(SIZE / dimensions.width, SIZE / dimensions.height) : 1;
  const scale = fit * zoom;
  const bounds = () => {
    const image = imageRef.current;
    if (!image) return { x: 0, y: 0 };
    return { x: Math.max(0, (image.naturalWidth * scale - SIZE) / 2), y: Math.max(0, (image.naturalHeight * scale - SIZE) / 2) };
  };
  const finish = () => {
    const image = imageRef.current;
    if (!image || !dimensions.width || !dimensions.height) return;
    const canvas = document.createElement("canvas");
    canvas.width = SIZE;
    canvas.height = SIZE;
    const context = canvas.getContext("2d");
    if (!context) { onError("无法处理这张图片，请换一张图片再试"); return; }
    const cropX = (image.naturalWidth * scale - SIZE) / 2 - offset.x;
    const cropY = (image.naturalHeight * scale - SIZE) / 2 - offset.y;
    context.drawImage(image, cropX / scale, cropY / scale, SIZE / scale, SIZE / scale, 0, 0, SIZE, SIZE);
    canvas.toBlob((blob) => {
      if (blob) onAccept(new File([blob], file.name.replace(/\.[^.]+$/, "") + ".png", { type: "image/png" }), file);
      else onError("无法生成裁切后的图片，请重试");
    }, "image/png");
  };

  return <section className="avatar-cropper" aria-label="调整头像裁切">
    <div
      className={`avatar-crop-window crop-${shape}${dragging ? " is-dragging" : ""}`}
      onPointerDown={(event) => { if (event.button !== 0) return; dragRef.current = { x: event.clientX, y: event.clientY, offsetX: offset.x, offsetY: offset.y }; event.currentTarget.setPointerCapture(event.pointerId); setDragging(true); }}
      onPointerMove={(event) => {
        if (!dragRef.current) return;
        const boundsNow = bounds();
        const rect = event.currentTarget.getBoundingClientRect();
        const dx = (event.clientX - dragRef.current.x) * SIZE / rect.width;
        const dy = (event.clientY - dragRef.current.y) * SIZE / rect.height;
        setOffset({ x: Math.max(-boundsNow.x, Math.min(boundsNow.x, dragRef.current.offsetX + dx)), y: Math.max(-boundsNow.y, Math.min(boundsNow.y, dragRef.current.offsetY + dy)) });
      }}
      onPointerUp={() => { dragRef.current = null; setDragging(false); }}
      onPointerCancel={() => { dragRef.current = null; setDragging(false); }}
      onWheel={(event) => { event.preventDefault(); event.stopPropagation(); const nextZoom = Math.max(1, Math.min(3, zoom * (event.deltaY > 0 ? 0.92 : 1.08))); setZoom(nextZoom); const nextScale = fit * nextZoom; setOffset((current) => ({ x: Math.sign(current.x) * Math.min(Math.abs(current.x), Math.max(0, (dimensions.width * nextScale - SIZE) / 2)), y: Math.sign(current.y) * Math.min(Math.abs(current.y), Math.max(0, (dimensions.height * nextScale - SIZE) / 2)) })); }}
    >
      {source && <img
        ref={imageRef}
        src={source}
        alt="头像裁切预览"
        draggable={false}
        onLoad={(event) => { setDimensions({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight }); setOffset({ x: 0, y: 0 }); }}
        style={{ width: dimensions.width ? dimensions.width * scale : undefined, height: dimensions.height ? dimensions.height * scale : undefined, transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))` }}
      />}
    </div>
    <label className="avatar-crop-zoom">缩放<input aria-label="缩放" type="range" min="1" max="3" step="0.01" value={zoom} onChange={(event) => { const nextZoom = Number(event.target.value); setZoom(nextZoom); const nextScale = fit * nextZoom; setOffset((current) => ({ x: Math.sign(current.x) * Math.min(Math.abs(current.x), Math.max(0, (dimensions.width * nextScale - SIZE) / 2)), y: Math.sign(current.y) * Math.min(Math.abs(current.y), Math.max(0, (dimensions.height * nextScale - SIZE) / 2)) })); }} /></label>
    <div className="avatar-crop-actions">
      <button type="button" className="ghost" onClick={onCancel}>取消</button>
      <button type="button" className="primary" disabled={!dimensions.width} onClick={finish}>保存</button>
    </div>
  </section>;
}
