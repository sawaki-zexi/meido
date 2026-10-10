import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { LandingPage } from "./LandingPage";

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("落地页背景", () => {
  it.each([
    [0, "/landing/night-sky.png"],
    [0.25, "/landing/sunlit-room.webp"],
    [0.5, "/landing/coastal-road.webp"],
    [0.75, "/landing/summer-meadow.webp"],
  ])("随机值 %s 选中 %s", (random, expected) => {
    vi.spyOn(Math, "random").mockReturnValue(random);
    const { container } = render(<LandingPage />);
    expect(container.querySelector<HTMLImageElement>(".landing-background")?.getAttribute("src")).toBe(expected);
  });

  it("进入对白后保持标题画面的背景", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const { container, getByRole } = render(<LandingPage />);
    const background = container.querySelector<HTMLImageElement>(".landing-background");
    fireEvent.click(getByRole("button", { name: "按任意键开始游戏" }));
    await waitFor(() => expect(container.querySelector(".dialogue-box")).toBeTruthy());
    expect(container.querySelector(".landing-background")).toBe(background);
    expect(background?.getAttribute("src")).toBe("/landing/coastal-road.webp");
  });
});
