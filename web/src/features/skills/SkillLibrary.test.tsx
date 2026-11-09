import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { SkillLibrary } from './SkillLibrary';
import type { OrdinarySkill, SkillCatalog } from '../../api/skills';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const entry = (overrides: Partial<OrdinarySkill> = {}): OrdinarySkill => ({ id: 'briefing', name: '工作简报', description: '适合整理工作；闲聊不适用',
  source: 'role', ref: 'skill:role:briefing:abc', scope: 'r1', contentHash: 'hash-1', enabled: true, effective: true,
  editable: true, status: 'available', policy: 'manual', legacyPolicy: 'explicit', problems: [], ignoredMetadata: [], supportingFiles: [], ...overrides });
const catalog = (skills = [entry()]): SkillCatalog => ({ enabled: false, revision: 0, skills, diagnostics: [],
  migration: skills.filter(item => item.effective).map(item => ({ ref: item.ref, name: item.name, from: item.legacyPolicy, to: item.policy })) });

it('预览逐角色迁移，再保存自动策略和停用；回退保留文件的含义清楚', async () => {
  let current = catalog();
  const { calls } = fakeBackend({
    'GET /api/roles/r1/skills': () => ({ body: current }),
    'PUT /api/roles/r1/skills/settings': init => {
      const change = JSON.parse(String(init?.body));
      expect(change.expectedRevision).toBe(current.revision);
      current = { ...current, revision: current.revision + 1, enabled: change.enabled ?? current.enabled,
        skills: current.skills.map(item => item.ref === change.ref ? { ...item, enabled: change.active ?? item.enabled, policy: change.policy ?? item.policy } : item) };
      return { body: current };
    },
  });
  const user = userEvent.setup(); render(<SkillLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览启用变化' }));
  expect(screen.getByText('只影响爱丽丝')).toBeVisible();
  expect(screen.getByText(/手动指定 → 仅手动指定/)).toBeVisible();
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '为爱丽丝启用自主选择' }));
  expect(await screen.findByText('自主选择已启用')).toBeVisible();
  await user.selectOptions(screen.getByRole('combobox', { name: '工作简报的使用方式' }), 'automatic');
  await waitFor(() => expect(current.skills[0].policy).toBe('automatic'));
  await waitFor(() => expect(screen.getByLabelText('启用 工作简报')).toBeEnabled());
  await user.click(screen.getByLabelText('启用 工作简报'));
  await waitFor(() => expect(current.skills[0].enabled).toBe(false));
  await waitFor(() => expect(screen.getByRole('button', { name: '回退旧触发方式' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '回退旧触发方式' }));
  expect(await screen.findByText('已回退旧触发方式，保存的技能和资源仍保留。')).toBeVisible();
});

it('导入后明确保存并核验，编辑冲突保留草稿，公共发布需要明确操作', async () => {
  let current = catalog([]);
  let raw = '---\nname: briefing\ndescription: 简报方法\n---\n先核验来源。';
  let fail = false;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/skills': () => ({ body: current }),
    'POST /api/roles/r1/skills': init => {
      if (fail) return { status: 409, body: { detail: '保存冲突：技能已变化，请重新读取' } };
      raw = JSON.parse(String(init?.body)).content;
      current = catalog([entry()]);
      return { body: { ref: entry().ref, content: raw, contentHash: 'hash-1', source: 'role', saved: true, verified: true, problems: [], ignoredMetadata: [] } };
    },
    'POST /api/roles/r1/skills/publish': () => ({ body: { published: true } }),
  });
  const user = userEvent.setup(); render(<SkillLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('沿用旧触发方式');
  const file = new File([raw], 'briefing.md', { type: 'text/markdown' });
  Object.defineProperty(file, 'text', { value: async () => raw });
  await user.upload(screen.getByLabelText('导入 Markdown'), file);
  expect(await screen.findByRole('textbox', { name: '技能内容' })).toHaveValue(raw);
  expect(calls.some(call => call.method === 'POST')).toBe(false);
  await user.click(screen.getByRole('button', { name: '保存并核验' }));
  expect(await screen.findByText('已保存并读回核验，下次对话可按当前使用方式复用。')).toBeVisible();
  fail = true;
  await user.type(screen.getByRole('textbox', { name: '技能内容' }), '\n增加核验');
  await user.click(screen.getByRole('button', { name: '保存并核验' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('保存冲突');
  expect(screen.getByRole('textbox', { name: '技能内容' })).toHaveValue(raw + '\n增加核验');
  expect(screen.getByRole('button', { name: '发布到公共库' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: '技能内容' }), { target: { value: raw } });
  await user.click(screen.getByRole('button', { name: '发布到公共库' }));
  expect(screen.getByRole('group', { name: '确认公共发布' })).toBeVisible();
  expect(calls.some(call => call.path.endsWith('/publish'))).toBe(false);
  await user.click(screen.getByRole('button', { name: '确认发布全部内容' }));
  expect(await screen.findByText('已发布到公共库，其他角色现在可以查看。')).toBeVisible();
  expect(calls.find(call => call.path.endsWith('/publish'))?.body).toEqual({ ref: entry().ref, expectedHash: 'hash-1' });
});

it('窄屏新建或选择技能会把编辑区带入视野并聚焦', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
  const scroll = vi.fn();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
  fakeBackend({
    'GET /api/roles/r1/skills': () => ({ body: catalog() }),
    'GET /api/roles/r1/skills/document\\?ref=skill%3Arole%3Abriefing%3Aabc': () => ({ body: {
      ref: entry().ref, content: '原有方法', contentHash: 'hash-1', source: 'role', ignoredMetadata: [] } }),
  });
  const user = userEvent.setup(); render(<SkillLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('沿用旧触发方式');
  await user.click(screen.getByRole('button', { name: '新建技能' }));
  expect(screen.getByRole('region', { name: '技能编辑区' })).toHaveFocus();
  expect(scroll).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
  await user.click(screen.getByRole('button', { name: '工作简报 适合整理工作；闲聊不适用' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: '技能内容' })).toHaveValue('原有方法'));
  expect(screen.getByRole('region', { name: '技能编辑区' })).toHaveFocus();
  expect(scroll).toHaveBeenCalledTimes(2);
});

it('切换角色后不接受旧角色迟到的私有正文', async () => {
  let release!: (reply: { body: unknown }) => void;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/skills': () => ({ body: catalog() }),
    'GET /api/roles/r2/skills': () => ({ body: catalog([entry({ id: 'new', ref: 'new', name: '新角色的方法' })]) }),
    'GET /api/roles/r1/skills/document\\?ref=skill%3Arole%3Abriefing%3Aabc': () => new Promise(resolve => { release = resolve; }),
  });
  const user = userEvent.setup();
  const view = render(<SkillLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '工作简报 适合整理工作；闲聊不适用' }));
  await waitFor(() => expect(calls.some(call => call.path.includes('/document'))).toBe(true));
  view.rerender(<SkillLibrary roleId="r2" roleName="贝拉" />);
  expect(await screen.findByRole('button', { name: '新角色的方法 适合整理工作；闲聊不适用' })).toBeVisible();
  await act(async () => release({ body: { ref: entry().ref, content: '旧角色的私有正文', source: 'role', contentHash: 'hash-1', ignoredMetadata: [] } }));
  expect(screen.queryByText('旧角色的私有正文')).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: '技能内容' })).not.toBeInTheDocument();
});

it('来源、覆盖项及缺资源显示具体原因，公共正文只读，私有删除检查实际版本', async () => {
  let current = catalog([entry({ status: 'unavailable', problems: ['missing supporting resources: references/source.md'] }),
    entry({ ref: 'builtin', source: 'builtin', status: 'shadowed', effective: false, editable: false, name: '内置简报' })]);
  const { calls } = fakeBackend({
    'GET /api/roles/r1/skills': () => ({ body: current }),
    'GET /api/roles/r1/skills/document\\?ref=builtin': () => ({ body: { ref: 'builtin', content: '内置完整说明', contentHash: 'builtin-hash', source: 'builtin', ignoredMetadata: [] } }),
    'GET /api/roles/r1/skills/document\\?ref=skill%3Arole%3Abriefing%3Aabc': () => ({ body: { ref: entry().ref, content: '私有完整说明', contentHash: 'hash-1', source: 'role', ignoredMetadata: [] } }),
    'POST /api/roles/r1/skills/remove': () => { current = catalog([]); return { body: { removed: true } }; },
  });
  const user = userEvent.setup(); render(<SkillLibrary roleId="r1" roleName="爱丽丝" />);
  expect(await screen.findByText('角色私有')).toBeVisible();
  expect(screen.getByText('被同名版本覆盖')).toBeVisible();
  expect(screen.getByText(/missing supporting resources/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: /内置简报/ }));
  expect(await screen.findByRole('textbox', { name: '技能内容' })).toHaveAttribute('readonly');
  expect(screen.queryByRole('button', { name: '移除私有技能' })).toBeNull();
  await user.click(screen.getByRole('button', { name: '工作简报 适合整理工作；闲聊不适用' }));
  await user.click(await screen.findByRole('button', { name: '移除私有技能' }));
  expect(calls.some(call => call.path.endsWith('/remove'))).toBe(false);
  await user.click(screen.getByRole('button', { name: '确认移除这个技能' }));
  expect(await screen.findByText('私有技能已移除。')).toBeVisible();
});
