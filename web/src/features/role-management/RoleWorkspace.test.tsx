import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('资料按四段单独展开编辑，工具栏保存和重置保留折叠字段及旧数据', async () => {
  const original = role({ id: 'r1', name: '爱丽丝' });
  let alice = { ...original, profile: { ...original.profile, nickname: '原称呼' } };
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'PUT /api/roles/r1': init => { alice = { ...alice, ...JSON.parse(String(init?.body)) }; return { body: { role: alice } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '角色' }));
  await user.click(await screen.findByRole('button', { name: '查看爱丽丝工作区' }));
  const workspace = within(screen.getByRole('region', { name: '爱丽丝的角色工作区' }));
  expect(workspace.getByRole('button', { name: '角色设定' })).toHaveAttribute('aria-expanded', 'true');
  for (const name of ['性格', '执行规则', '回复约束']) expect(workspace.getByRole('button', { name })).toHaveAttribute('aria-expanded', 'false');
  expect(workspace.queryByRole('button', { name: '上传头像' })).not.toBeInTheDocument();
  expect(workspace.getByRole('button', { name: '保存' })).toBeDisabled();
  await user.clear(workspace.getByRole('textbox', { name: '角色设定' }));
  await user.type(workspace.getByRole('textbox', { name: '角色设定' }), '更新后的故事');
  await user.click(workspace.getByRole('button', { name: '性格' }));
  expect(workspace.queryByRole('textbox', { name: '角色设定' })).not.toBeInTheDocument();
  await user.type(workspace.getByRole('textbox', { name: '性格' }), '温柔而坚定');
  await user.click(workspace.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(workspace.getByRole('button', { name: '保存' })).toBeDisabled());
  expect(calls.find(call => call.method === 'PUT')?.body).toMatchObject({ profile: { profile: '更新后的故事', personality: '温柔而坚定', nickname: '原称呼' } });
  await user.clear(workspace.getByRole('textbox', { name: '性格' }));
  await user.click(workspace.getByRole('button', { name: '重置' }));
  expect(workspace.getByRole('textbox', { name: '性格' })).toHaveValue('温柔而坚定');
  expect(workspace.getByRole('button', { name: '重置' })).toBeDisabled();
});

it("资料页不显示昵称，保存设定兼容旧称呼，重置保留已保存值并可返回对话", async () => {
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
  await waitFor(() => expect(within(workspace).getByRole("button", { name: "保存" })).toBeDisabled());
  await user.clear(profile);
  await user.type(profile, "未保存设定");
  await user.click(within(workspace).getByRole("button", { name: "重置" }));
  expect(within(workspace).getByRole("textbox", { name: "角色设定" })).toHaveValue("新的角色设定");
  await user.click(within(workspace).getByRole("button", { name: "返回对话" }));
  expect(await screen.findByText("原来的对话", { selector: ".bubble p" })).toBeVisible();
});

it('角色设定折叠后仍校验必填，定位到空白设定并保留其他草稿', async () => {
  const alice = role({ id: 'r1', name: '爱丽丝' });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: '角色资料' }));
  const workspace = within(screen.getByRole('region', { name: '爱丽丝的角色工作区' }));
  await user.clear(workspace.getByRole('textbox', { name: '角色设定' }));
  await user.click(workspace.getByRole('button', { name: '性格' }));
  await user.type(workspace.getByRole('textbox', { name: '性格' }), '未保存的性格');
  await user.click(workspace.getByRole('button', { name: '保存' }));
  expect(await workspace.findByRole('alert')).toHaveTextContent('请填写角色设定');
  expect(workspace.getByRole('textbox', { name: '角色设定' })).toHaveFocus();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
  await user.click(workspace.getByRole('button', { name: '性格' }));
  expect(workspace.getByRole('textbox', { name: '性格' })).toHaveValue('未保存的性格');
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

it('切换工作区标签保留未保存的能力更改，保存按钮只在能力页显示', async () => {
  const alice = role({ id: 'r1', name: '爱丽丝' });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1': () => ({ body: { role: alice } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r1/autonomy/settings': () => ({ body: { settings: { enabled: false, revision: 1 }, modelReady: true } }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: { enabled: false, packages: [], selectedPackageId: null, revision: 1 } }),
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: [], total: 0 } }),
    'GET /api/roles/r1/memory-admin/filters': () => ({ body: { domains: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '角色' }));
  await user.click(await screen.findByRole('button', { name: '查看爱丽丝工作区' }));
  const workspace = within(screen.getByRole('region', { name: '爱丽丝的角色工作区' }));
  await user.click(workspace.getByRole('tab', { name: '能力' }));
  await waitFor(() => expect(workspace.getByRole('switch', { name: '主动推送' })).toBeEnabled());
  await user.click(workspace.getByRole('switch', { name: '主动推送' }));
  await user.click(workspace.getByRole('tab', { name: '记忆' }));
  expect(workspace.queryByRole('button', { name: '保存' })).not.toBeInTheDocument();
  await user.click(workspace.getByRole('tab', { name: '能力' }));
  expect(workspace.getByRole('switch', { name: '主动推送' })).toHaveAttribute('aria-checked', 'true');
  expect(workspace.getByRole('button', { name: '保存' })).toBeEnabled();
  expect(calls.filter(call => call.method !== 'GET')).toEqual([]);
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
  expect(await screen.findByText("爱丽丝的回复", { selector: ".bubble p" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(screen.getByRole("button", { name: "查看贝拉工作区" }));
  await user.click(screen.getByRole("button", { name: "去聊天" }));
  expect(await screen.findByText("贝拉的历史", { selector: ".bubble p" })).toBeInTheDocument();
  expect(screen.queryByText("爱丽丝的回复", { selector: ".bubble p" })).not.toBeInTheDocument();
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
  await user.click(within(workspace).getByRole("button", { name: "更换形象" }));
  const input = within(workspace).getByLabelText(inputName);
  const file = new File(["original"], "picture.png", { type: "image/png" });
  await user.upload(input, file);
  const crop = within(workspace).queryByRole("region", { name: cropName });
  expect(crop).not.toBeNull();
  await user.click(within(crop!).getByRole("button", { name: "取消" }));
  expect(within(workspace).queryByRole("region", { name: cropName })).not.toBeInTheDocument();
  expect(within(workspace).getByRole("button", { name: "保存" })).toBeDisabled();
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

it('普通角色工作区直接显示素材库，返回与重复进入不会复活旧预览，也不会读取生成历史或开启 Drift', async () => {
  const alice = role({ id: 'r1', name: '爱丽丝' });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r1/materials': () => ({ body: { revision: 'revision-1', storageLabel: '当前角色工作区 / workspace/memes', categories: [{ name: '开心', description: '', count: 1, enabled: true }], materials: [{ id: 'm1', name: '你好.gif', category: '开心', description: '挥手打招呼', aliases: [], format: 'GIF', mediaType: 'image/gif', size: 100, width: 80, height: 40, animated: true, source: 'legacy', enabled: true, imageUrl: '/api/roles/r1/materials/m1/image' }] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '角色' }));
  await user.click(await screen.findByRole('button', { name: '查看爱丽丝工作区' }));
  const tab = screen.getByRole('tab', { name: '素材库' });
  expect(tab).toBeVisible();
  await user.click(tab);
  expect(tab).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByRole('tabpanel', { name: '素材库' })).toBeVisible();
  expect(await screen.findByText('你好.gif')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '预览你好.gif' }));
  expect(screen.getByRole('dialog', { name: '你好.gif' })).toBeVisible();
  await user.keyboard('{Escape}');
  expect(screen.getByRole('button', { name: '预览你好.gif' })).toHaveFocus();
  await user.click(screen.getByRole('button', { name: '返回角色' }));
  expect(screen.queryByRole('region', { name: '爱丽丝的素材库' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '查看爱丽丝工作区' }));
  expect(screen.getByRole('tab', { name: '资料' })).toHaveAttribute('aria-selected', 'true');
  await user.click(screen.getByRole('tab', { name: '素材库' }));
  expect(await screen.findByText('你好.gif')).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(calls.filter(call => call.path.endsWith('/materials'))).toHaveLength(2);
  expect(calls.some(call => call.path.includes('/generations') || call.path.includes('/autonomy'))).toBe(false);
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});
