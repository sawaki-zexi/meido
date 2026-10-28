import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { LandingPage } from "./LandingPage";

beforeEach(() => {
  window.localStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
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
  vi.unstubAllEnvs();
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

it("在 GitHub Pages 子路径浏览时从同一站点加载背景、立绘和音乐", () => {
  vi.stubEnv("BASE_URL", "/meido/");
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const { container, getByRole } = render(<LandingPage />);
  expect(container.querySelector(".landing-background")).toHaveAttribute("src", "/meido/landing/coastal-road.webp");
  expect(getByRole("img", { name: "Meido 的女仆讲解角色" })).toHaveAttribute("src", "/meido/landing/elysia-greeting.png");
  expect(container.querySelector("audio")).toHaveAttribute("src", "/meido/landing/neko-ni-michibikarete.mp3");
  fireEvent.click(getByRole("button", { name: "按任意键开始游戏" }));
  fireEvent.click(getByRole("button", { name: "跳过当前段落" }));
  fireEvent.click(getByRole("button", { name: /角色$/ }));
  expect(getByRole("img", { name: "Meido 的女仆讲解角色" })).toHaveAttribute("src", "/meido/landing/elysia-attentive.png");
  fireEvent.click(getByRole("button", { name: "跳过当前段落" }));
  fireEvent.click(getByRole("button", { name: /没什么想问的了/ }));
  expect(getByRole("img", { name: "Meido 的女仆讲解角色" })).toHaveAttribute("src", "/meido/landing/elysia-smile.png");
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

describe("落地页背景音乐", () => {
  it("右上角开关独立控制音乐，关闭后开始游戏仍保持关闭，并记住开关状态", async () => {
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    const pause = vi.mocked(HTMLMediaElement.prototype.pause);
    const { getByRole, queryByRole, unmount } = render(<LandingPage />);
    const toggle = getByRole("button", { name: "关闭背景音乐" });
    fireEvent.keyDown(toggle, { key: "Enter" });
    expect(getByRole("button", { name: "按任意键开始游戏" })).toBeTruthy();
    fireEvent.click(toggle);
    expect(pause).toHaveBeenCalledOnce();
    expect(play).not.toHaveBeenCalled();
    expect(getByRole("button", { name: "开启背景音乐" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(getByRole("button", { name: "按任意键开始游戏" }));
    await waitFor(() => expect(queryByRole("button", { name: "打开设置" })).toBeTruthy());
    expect(play).not.toHaveBeenCalled();
    fireEvent.click(getByRole("button", { name: "开启背景音乐" }));
    expect(play).toHaveBeenCalledOnce();
    fireEvent.click(getByRole("button", { name: "关闭背景音乐" }));
    expect(pause).toHaveBeenCalledTimes(2);
    unmount();
    const nextVisit = render(<LandingPage />);
    expect(nextVisit.getByRole("button", { name: "开启背景音乐" })).toHaveAttribute("aria-pressed", "false");
    expect(play).toHaveBeenCalledOnce();
  });

  it("访客开始后播放循环音乐，设置音量立即生效并保留偏好", async () => {
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    const { container, getByRole, unmount } = render(<LandingPage />);
    const music = container.querySelector<HTMLAudioElement>("audio");
    expect(music?.getAttribute("src")).toBe("/landing/neko-ni-michibikarete.mp3");
    expect(music?.loop).toBe(true);
    expect(play).not.toHaveBeenCalled();

    fireEvent.click(getByRole("button", { name: "按任意键开始游戏" }));
    expect(play).toHaveBeenCalledOnce();
    expect(music?.volume).toBe(0.35);

    await waitFor(() => expect(getByRole("button", { name: "打开设置" })).toBeTruthy());
    fireEvent.click(getByRole("button", { name: "打开设置" }));
    fireEvent.change(getByRole("slider", { name: "背景音乐音量" }), { target: { value: "0" } });
    expect(music?.volume).toBe(0);
    fireEvent.change(getByRole("slider", { name: "背景音乐音量" }), { target: { value: "65" } });
    expect(music?.volume).toBe(0.65);
    expect(window.localStorage.getItem("meido:landing-music-volume")).toBe("65");

    unmount();
    const nextVisit = render(<LandingPage />);
    expect(nextVisit.container.querySelector<HTMLAudioElement>("audio")?.volume).toBe(0.65);
    expect(play).toHaveBeenCalledOnce();
  });
});
