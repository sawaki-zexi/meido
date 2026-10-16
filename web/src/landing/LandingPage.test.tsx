import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { LandingPage } from "./LandingPage";

beforeEach(() => {
  window.localStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
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

describe("落地页背景音乐", () => {
  it("访客开始后播放循环音乐，设置音量立即生效并保留偏好", async () => {
    const play = vi.mocked(HTMLMediaElement.prototype.play);
    const { container, getByRole, unmount } = render(<LandingPage />);
    const music = container.querySelector<HTMLAudioElement>("audio");
    expect(music?.getAttribute("src")).toBe("/landing/summer-adventure.mp3");
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
