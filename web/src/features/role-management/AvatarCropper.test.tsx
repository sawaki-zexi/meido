import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AvatarCropper } from "./AvatarCropper";

const original = new File(["portrait"], "portrait.jpg", { type: "image/jpeg" });

function loadImage(width: number, height: number) {
  const image = screen.getByRole("img");
  Object.defineProperties(image, {
    naturalWidth: { configurable: true, value: width },
    naturalHeight: { configurable: true, value: height },
  });
  fireEvent.load(image);
  return image;
}

function captureExport() {
  const drawImage = vi.fn();
  let canvas: HTMLCanvasElement | undefined;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(function (this: HTMLCanvasElement, callback) {
    canvas = this;
    callback(new Blob(["cropped"], { type: "image/png" }));
  });
  return { drawImage, canvas: () => canvas };
}

beforeEach(() => {
  vi.stubGlobal("URL", { createObjectURL: vi.fn((file: File) => `blob:${file.name}`), revokeObjectURL: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("角色图片裁切", () => {
  it.each([
    ["card", 1280, 640, 384, 0, 512, 640, 640],
    ["card", 512, 1280, 0, 320, 512, 640, 640],
    ["circle", 1280, 640, 320, 0, 640, 640, 512],
    ["square", 512, 1280, 0, 384, 512, 512, 512],
  ] as const)("%s 裁切 %i×%i 原图时按目标比例导出居中区域", async (shape, width, height, x, y, cropWidth, cropHeight, outputHeight) => {
    const user = userEvent.setup();
    const onAccept = vi.fn();
    const output = captureExport();
    render(<AvatarCropper file={original} shape={shape} onAccept={onAccept} onCancel={vi.fn()} onError={vi.fn()} />);
    const image = loadImage(width, height);

    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(output.canvas()).toMatchObject({ width: 512, height: outputHeight });
    expect(output.drawImage).toHaveBeenCalledWith(image, x, y, cropWidth, cropHeight, 0, 0, 512, outputHeight);
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ name: "portrait.png", type: "image/png" }), original);
  });

  it("更换原图后重置缩放，并在新图加载完成前禁止保存", async () => {
    const user = userEvent.setup();
    const onAccept = vi.fn();
    const output = captureExport();
    const props = { onAccept, onCancel: vi.fn(), onError: vi.fn() };
    const view = render(<AvatarCropper file={original} {...props} />);
    loadImage(512, 512);
    fireEvent.change(screen.getByRole("slider", { name: "缩放" }), { target: { value: "2" } });
    const replacement = new File(["replacement"], "replacement.jpg", { type: "image/jpeg" });

    view.rerender(<AvatarCropper file={replacement} {...props} />);

    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(screen.getByRole("slider", { name: "缩放" })).toHaveValue("1");
    const image = loadImage(512, 1024);
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(output.drawImage).toHaveBeenCalledWith(image, 0, 256, 512, 512, 0, 0, 512, 512);
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ name: "replacement.png" }), replacement);
  });

  it("图片加载失败时反馈错误并禁止保存无效裁切", async () => {
    const user = userEvent.setup();
    const onError = vi.fn();
    const onAccept = vi.fn();
    render(<AvatarCropper file={original} onAccept={onAccept} onCancel={vi.fn()} onError={onError} />);
    const image = loadImage(512, 512);

    fireEvent.error(image);

    expect(onError).toHaveBeenCalledWith(expect.stringContaining("图片"));
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("在缩小的卡片视口拖动到边缘后，缩回原尺寸仍完整覆盖裁切区域", async () => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    const user = userEvent.setup();
    const output = captureExport();
    render(<AvatarCropper file={original} shape="card" onAccept={vi.fn()} onCancel={vi.fn()} onError={vi.fn()} />);
    const image = loadImage(512, 640);
    const viewport = image.parentElement!;
    viewport.setPointerCapture = vi.fn();
    vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 256, 320));
    fireEvent.change(screen.getByRole("slider", { name: "缩放" }), { target: { value: "2" } });

    fireEvent.pointerDown(viewport, { button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(viewport, { clientX: 128, clientY: 160 });
    fireEvent.pointerUp(viewport);
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(output.drawImage).toHaveBeenLastCalledWith(image, 0, 0, 256, 320, 0, 0, 512, 640);
    fireEvent.change(screen.getByRole("slider", { name: "缩放" }), { target: { value: "1" } });
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(output.drawImage).toHaveBeenLastCalledWith(image, 0, 0, 512, 640, 0, 0, 512, 640);
  });

  it("取消裁切时保留原图，不接受裁切结果", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onAccept = vi.fn();
    render(<AvatarCropper file={original} onAccept={onAccept} onCancel={onCancel} onError={vi.fn()} />);
    loadImage(512, 512);

    await user.click(screen.getByRole("button", { name: "取消" }));

    expect(onCancel).toHaveBeenCalledOnce();
    expect(onAccept).not.toHaveBeenCalled();
  });
});
