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

it("访客能浏览六个模块并返回选项，结束后下载项目源码", () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const { container, getByRole } = render(<LandingPage />);
  fireEvent.click(getByRole("button", { name: "按任意键开始游戏" }));
  expect(container.querySelector(".speaker-name")?.textContent).toContain("Hiyori");
  fireEvent.click(getByRole("button", { name: "跳过当前段落" }));
  const modules = ["角色", "记忆", "工具", "待办", "邮件", "主人资料"];
  expect(Array.from(container.querySelectorAll(".choice-button"), (button) => button.children[1].textContent)).toEqual([...modules, "没什么想问的了"]);
  for (const name of modules) {
    fireEvent.click(getByRole("button", { name: new RegExp(`${name}$`) }));
    for (let i = 0; i < 3 && !container.querySelector(".dialogue-text")?.textContent?.includes("客人还想了解些什么呢？"); i++) {
      fireEvent.click(container.querySelector(".dialogue-box")!);
    }
    expect(container.querySelector(".dialogue-text")?.textContent).toContain("客人还想了解些什么呢？");
    fireEvent.click(container.querySelector(".dialogue-box")!);
    expect(getByRole("button", { name: /没什么想问的了/ })).toBeTruthy();
  }
  fireEvent.click(getByRole("button", { name: /没什么想问的了/ }));
  fireEvent.click(getByRole("button", { name: "跳过当前段落" }));
  expect(getByRole("link", { name: /下载项目源码/ })).toHaveAttribute("href", "https://github.com/sawaki-zexi/meido/archive/refs/heads/main.zip");
});
