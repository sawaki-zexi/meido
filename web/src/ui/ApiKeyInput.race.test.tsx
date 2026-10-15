import { act, fireEvent, waitFor } from "@testing-library/react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ApiKeyInput } from "./ApiKeyInput";

it("honors an explicit click as soon as the saved-key control is committed", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let complete!: (value: string) => void;
  const onReveal = vi.fn(() => new Promise<string>((resolve) => { complete = resolve; }));
  const onChange = vi.fn();
  // A raw concurrent root lets the click occur after DOM commit and before
  // passive effects. RTL's render() flushes those effects and hides this race.
  const committed = new Promise<void>((resolve) => {
    const observer = new MutationObserver(() => {
      const button = container.querySelector("button");
      if (!button) return;
      observer.disconnect();
      fireEvent.click(button);
      resolve();
    });
    observer.observe(container, { childList: true, subtree: true });
  });
  try {
    root.render(<ApiKeyInput id="immediate-key" label="API Key" value="" configured onReveal={onReveal} onChange={onChange} />);
    await committed;
    expect(onReveal).toHaveBeenCalledOnce();
    await act(async () => { complete("explicit-click-secret"); });
    await waitFor(() => expect(container.querySelector("input")).toHaveValue("explicit-click-secret"));
    expect(onChange).not.toHaveBeenCalled();
  } finally {
    act(() => root.unmount());
    container.remove();
  }
});
