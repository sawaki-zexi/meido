import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiKeyInput } from "./ApiKeyInput";

afterEach(cleanup);

describe("ApiKeyInput", () => {
  it("shows a fixed saved mask without copying it into the draft or reading the saved key", () => {
    const onChange = vi.fn();
    const onReveal = vi.fn<() => Promise<string>>();
    render(<ApiKeyInput id="key" label="API Key" value="" onChange={onChange} configured onReveal={onReveal} />);

    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("placeholder", "••••••");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();
    expect(onChange).not.toHaveBeenCalled();
    expect(onReveal).not.toHaveBeenCalled();
  });

  it("toggles a draft without reading or changing the saved key", () => {
    const onChange = vi.fn();
    const onReveal = vi.fn<() => Promise<string>>();
    render(<ApiKeyInput id="key" label="API Key" value="draft-example" onChange={onChange} configured onReveal={onReveal} />);

    const input = screen.getByLabelText("API Key");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveValue("draft-example");
    expect(screen.getByRole("button", { name: "隐藏API Key" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "隐藏API Key" }));
    expect(input).toHaveAttribute("type", "password");
    expect(onChange).not.toHaveBeenCalled();
    expect(onReveal).not.toHaveBeenCalled();
  });

  it("reads a saved key only on request, clears it on hide, and keeps it out of the draft", async () => {
    let completeReveal!: (value: string) => void;
    const onReveal = vi.fn(() => new Promise<string>((resolve) => { completeReveal = resolve; }));
    const onChange = vi.fn();
    render(<ApiKeyInput id="key" label="API Key" value="" onChange={onChange} configured onReveal={onReveal} />);

    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    expect(screen.getByRole("status")).toHaveTextContent("正在读取API Key");
    expect(screen.getByRole("button", { name: "读取API Key…" })).toBeDisabled();
    expect(screen.getByLabelText("API Key")).toHaveValue("");

    await act(async () => { completeReveal("stored-example"); });
    expect(screen.getByLabelText("API Key")).toHaveValue("stored-example");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "text");
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "隐藏API Key" }));
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    expect(onReveal).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    await act(async () => { completeReveal("stored-example"); });
  });

  it("shows a read failure below the field and allows retry", async () => {
    const onReveal = vi.fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error("本地后端暂时不可用"))
      .mockResolvedValueOnce("stored-example");
    const onChange = vi.fn();
    render(<ApiKeyInput id="key" label="API Key" value="" onChange={onChange} configured onReveal={onReveal} />);

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "显示API Key" })); });
    expect(screen.getByRole("alert")).toHaveTextContent("本地后端暂时不可用");
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "显示API Key" })); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("API Key")).toHaveValue("stored-example");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("hides a revealed saved key while saving or testing and keeps it hidden afterwards", async () => {
    const props = { id: "key", label: "API Key", value: "", onChange: vi.fn(), configured: true, onReveal: vi.fn().mockResolvedValue("stored-example") };
    const { rerender } = render(<ApiKeyInput {...props} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "显示API Key" })); });
    expect(screen.getByLabelText("API Key")).toHaveValue("stored-example");

    rerender(<ApiKeyInput {...props} disabled />);
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeDisabled();
    rerender(<ApiKeyInput {...props} disabled={false} />);
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();
  });

  it("ignores an earlier read after saving or testing starts, even after the field is enabled again", async () => {
    let completeReveal!: (value: string) => void;
    const onReveal = vi.fn(() => new Promise<string>((resolve) => { completeReveal = resolve; }));
    const props = { id: "key", label: "API Key", value: "", onChange: vi.fn(), configured: true, onReveal };
    const { rerender } = render(<ApiKeyInput {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    rerender(<ApiKeyInput {...props} disabled />);
    rerender(<ApiKeyInput {...props} disabled={false} />);

    await act(async () => { completeReveal("stale-example"); });
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps a new draft masked when an earlier saved-key read completes", async () => {
    let completeReveal!: (value: string) => void;
    const onReveal = () => new Promise<string>((resolve) => { completeReveal = resolve; });
    const onChange = vi.fn();
    const props = { id: "key", label: "API Key", onChange, configured: true, onReveal };
    const { rerender } = render(<ApiKeyInput {...props} value="" />);
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "new-draft" } });
    expect(onChange).toHaveBeenCalledWith("new-draft");
    rerender(<ApiKeyInput {...props} value="new-draft" />);

    await act(async () => { completeReveal("stale-example"); });
    expect(screen.getByLabelText("API Key")).toHaveValue("new-draft");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();
  });

  it("resets visibility when a parent clears the draft before entering another key", () => {
    const props = { id: "key", label: "API Key", onChange: vi.fn(), configured: false, onReveal: vi.fn<() => Promise<string>>() };
    const { rerender } = render(<ApiKeyInput {...props} value="draft-example" />);
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "text");

    rerender(<ApiKeyInput {...props} value="" />);
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeDisabled();
    rerender(<ApiKeyInput {...props} value="another-draft" />);
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
  });

  it.each(["resolve", "reject"])("ignores a read that later %ss after its connection is unmounted", async (outcome) => {
    let resolveReveal!: (value: string) => void;
    let rejectReveal!: (cause: Error) => void;
    const onReveal = () => new Promise<string>((resolve, reject) => { resolveReveal = resolve; rejectReveal = reject; });
    const props = { id: "key", label: "API Key", value: "", onChange: vi.fn(), configured: true, onReveal };
    const { rerender } = render(<ApiKeyInput key="first-connection" {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    rerender(<ApiKeyInput key="second-connection" {...props} />);

    await act(async () => {
      if (outcome === "resolve") resolveReveal("stale-example");
      else rejectReveal(new Error("旧连接读取失败"));
    });
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
