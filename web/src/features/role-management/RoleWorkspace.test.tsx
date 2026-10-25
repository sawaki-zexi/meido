import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("资料页不显示昵称，保存设定兼容旧称呼，取消保留已保存值并可返回对话", async () => {
  const original = role({ id: "r1", name: "爱丽丝" });
  let alice = { ...original, profile: { ...original.profile, nickname: "旧称呼" } };
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [message({ id: "old", role: "assistant", content: "原来的对话" })] } }),
    "PUT /api/roles/r1": init => { alice = { ...alice, ...JSON.parse(String(init?.body)) }; return { body: { role: alice } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "角色资料" }));
  const workspace = within(screen.getByRole("main")).getByRole("region", { name: "爱丽丝的角色工作区" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "记忆" })).toBeVisible();
  expect(within(workspace).queryByLabelText("昵称")).not.toBeInTheDocument();
  expect(within(workspace).queryByText("对你的称呼")).not.toBeInTheDocument();
  const profile = within(workspace).getByRole("textbox", { name: "角色设定" });
  await user.clear(profile);
  await user.type(profile, "新的角色设定");
  await user.click(within(workspace).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.find(call => call.method === "PUT")?.body).toMatchObject({ profile: { profile: "新的角色设定", nickname: "旧称呼" } }));
  await waitFor(() => expect(within(workspace).getByRole("button", { name: "保存" })).toBeEnabled());
  await user.clear(profile);
  await user.type(profile, "未保存设定");
  await user.click(within(workspace).getByRole("button", { name: "取消" }));
  expect(within(workspace).getByRole("textbox", { name: "角色设定" })).toHaveValue("新的角色设定");
  await user.click(within(workspace).getByRole("button", { name: "返回对话" }));
  expect(await screen.findByText("原来的对话")).toBeVisible();
});

it("刷新后全页恢复新建草稿，已有对话加载完成也不覆盖草稿页", async () => {
  sessionStorage.setItem("meido:create-role-draft", JSON.stringify({ name: "未完成角色", profile: "她的设定" }));
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "爱丽丝" })] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
  });
  render(<App />);
  await waitFor(() => expect(calls.some(call => call.path === "/api/roles/r1/session")).toBe(true));
  const create = within(screen.getByRole("main")).getByRole("region", { name: "创建角色" });
  expect(within(create).getByLabelText(/名称/)).toHaveValue("未完成角色");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("从角色卡进入 Shiori 风格工作区，提供记忆、能力和账号页且不显示好感度", async () => {
  const alice = role({ id: "r1", name: "爱丽丝", description: "安静的女仆" });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/roles/r1/memories.*": () => ({ body: { memories: [], total: 0 } }),
    "GET /api/roles/r1/tools": () => ({ body: { roleId: "r1", sessionKey: "role:r1", preview: true, tools: [], plugins: [] } }),
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
    "GET /api/channels/settings": () => ({ body: { channels: [] } }),
    "GET /api/roles/r1/channels/events": () => ({ stream: [] }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(await screen.findByRole("button", { name: "查看爱丽丝工作区" }));

  const workspace = await screen.findByRole("region", { name: "爱丽丝的角色工作区" });
  expect(within(workspace).getByRole("tab", { name: "资料" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "记忆" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "能力" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "账号" })).toBeInTheDocument();
  expect(within(workspace).queryByRole("tab", { name: "好感度" })).not.toBeInTheDocument();

  await user.click(within(workspace).getByRole("tab", { name: "记忆" }));
  expect(await within(workspace).findByRole("region", { name: "爱丽丝的记忆" })).toBeInTheDocument();
  await user.click(within(workspace).getByRole("tab", { name: "能力" }));
  expect(await within(workspace).findByRole("region", { name: "角色能力" })).toBeInTheDocument();
});

it.each(["去聊天", "记忆来源"])("回复期间不能通过工作区%s切换会话，结束后可以切换", async (entry) => {
  const alice = role({ id: "r1", name: "爱丽丝" });
  const bella = role({ id: "r2", name: "贝拉" });
  const origin = { kind: "message", sessionKey: "role:r2", messageIds: ["b1"], messageRange: null, stableSourceKey: "message:b1" };
  const memory = { id: "memory-b", roleId: "r2", summary: "贝拉的记忆", memoryType: "fact", status: "active", extra: {},
    createdAt: "2026-10-24T00:00:00Z", updatedAt: "2026-10-24T00:00:00Z", happenedAt: null,
    sourceRef: { ...origin, sources: [origin], sourceKeys: [] } };
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/roles/r2/session": () => ({ body: { session: { sessionKey: "role:r2", roleId: "r2" }, messages: [message({ id: "b1", sessionKey: "role:r2", role: "assistant", content: "贝拉的历史" })] } }),
    "GET /api/roles/r2/memories.*": () => ({ body: { memories: [memory], total: 1 } }),
    "POST /api/roles/r1/messages": () => ({ stream }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "爱丽丝" });
  await user.type(screen.getByLabelText("消息内容"), "你好{Enter}");
  await screen.findByRole("button", { name: "停止回复" });
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(screen.getByRole("button", { name: "查看贝拉工作区" }));
  const workspace = screen.getByRole("region", { name: "贝拉的角色工作区" });
  if (entry === "去聊天") {
    const chatButton = within(workspace).getByRole("button", { name: "去聊天" });
    expect(chatButton).toBeDisabled();
    await user.click(chatButton);
  } else {
    await user.click(within(workspace).getByRole("tab", { name: "记忆" }));
    await user.click(await within(workspace).findByRole("button", { name: "查看记忆：贝拉的记忆" }));
    await user.click(within(workspace).getByRole("button", { name: "定位到消息" }));
  }
  expect(workspace).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "对话" }));
  expect(screen.getByRole("heading", { name: "爱丽丝" })).toBeInTheDocument();
  await act(async () => {
    const encoder = new TextEncoder();
    controller.enqueue(encoder.encode(sse("assistant_generation_started", { messageId: "a1" })));
    controller.enqueue(encoder.encode(sse("assistant_completed", { message: message({ id: "a1", role: "assistant", content: "爱丽丝的回复" }) })));
  });
  expect(await screen.findByText("爱丽丝的回复")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(screen.getByRole("button", { name: "查看贝拉工作区" }));
  await user.click(screen.getByRole("button", { name: "去聊天" }));
  expect(await screen.findByText("贝拉的历史")).toBeInTheDocument();
  expect(screen.queryByText("爱丽丝的回复")).not.toBeInTheDocument();
});

it.each([
  ["角色头像", "调整头像裁切", "头像裁切预览", "上传头像", "avatar", "avatarUrl"],
  ["卡片图片", "调整卡片裁切", "卡片裁切预览", "上传卡片图片", "card-image", "cardImageUrl"],
] as const)("工作区%s可以取消裁切、重新裁切并保存", async (inputName, cropName, previewName, uploadName, endpoint, imageField) => {
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL() { return "blob:workspace-preview"; }
    static revokeObjectURL() {}
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(callback => callback(new Blob(["cropped"], { type: "image/png" })));
  let saved = role({ id: "r1", name: "爱丽丝" });
  const imageUrl = `/api/roles/r1/${endpoint}?saved=1`;
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": () => ({ body: { role: saved } }),
    [`POST /api/roles/r1/${endpoint}`]: () => { saved = { ...saved, [imageField]: imageUrl }; return { body: { role: saved } }; },
    "POST /api/roles/r1/avatar-original": () => ({ body: { role: saved } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(await screen.findByRole("button", { name: "查看爱丽丝工作区" }));
  const workspace = screen.getByRole("region", { name: "爱丽丝的角色工作区" });
  await user.click(within(workspace).getByRole("button", { name: "编辑资料" }));
  const input = within(workspace).getByLabelText(inputName);
  const file = new File(["original"], "picture.png", { type: "image/png" });
  await user.upload(input, file);
  const crop = within(workspace).queryByRole("region", { name: cropName });
  expect(crop).not.toBeNull();
  await user.click(within(crop!).getByRole("button", { name: "取消" }));
  expect(within(workspace).queryByRole("region", { name: cropName })).not.toBeInTheDocument();
  expect(within(workspace).getByRole("button", { name: "保存" })).toBeEnabled();
  await user.upload(input, file);
  const secondCrop = within(workspace).getByRole("region", { name: cropName });
  const image = within(secondCrop).getByRole("img", { name: previewName });
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: 800 }, naturalHeight: { configurable: true, value: 600 } });
  fireEvent.load(image);
  await user.click(within(secondCrop).getByRole("button", { name: "保存" }));
  expect(within(workspace).queryByRole("region", { name: cropName })).not.toBeInTheDocument();
  await user.click(within(workspace).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(within(workspace).getByRole("button", { name: uploadName }).querySelector("img")).toHaveAttribute("src", imageUrl));
});
