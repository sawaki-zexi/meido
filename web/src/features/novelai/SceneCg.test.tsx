import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, message, role } from '../../test/fakeBackend';
import { App } from '../../App';
import type { AgentToolConfig } from '../../api/types';
import type { NovelaiRoleConfig } from '../../api/novelai';
import { draftFromRole, payloadFromDraft } from '../role-management/draft';
import { NovelaiRoleSettings } from './NovelaiRoleSettings';
import { GenerationDrawer } from './GenerationDrawer';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });
const service = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
  allowedModels: ['nai'], defaultModel: 'nai', allowedSamplers: ['k_euler'], defaultSampler: 'k_euler',
  maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
const models = [{ id: 'helper', provider: 'openai', model: 'scene-observer', api: 'openai-completions' }];

it('enables CG independently with an explicit model and renews consent when asset or rating permission changes', async () => {
  fakeBackend({ 'GET /api/services/novelai': () => ({ body: service }),
    'GET /api/model/configurations': () => ({ body: { configurations: models, activeId: 'helper' } }) });
  let saved: ReturnType<typeof payloadFromDraft> | undefined;
  function Harness() {
    const [draft, setDraft] = useState(draftFromRole(role({ id: 'r1', name: '诗织' })));
    return <form onSubmit={(event) => { event.preventDefault(); saved = payloadFromDraft(draft); }}>
      <NovelaiRoleSettings draft={draft} onChange={setDraft} /><button>保存角色</button></form>;
  }
  const user = userEvent.setup(); render(<Harness />);
  const cg = await screen.findByRole('checkbox', { name: '允许主人回合场景 CG' });
  expect(cg).not.toBeChecked();
  await screen.findByText(/账户每日最多 20 次/);
  await user.click(cg);
  expect(screen.getByRole('combobox', { name: 'CG 辅助模型连接' })).toHaveValue('');
  await user.click(screen.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toBeUndefined();
  await user.selectOptions(screen.getByRole('combobox', { name: 'CG 辅助模型连接' }), 'helper');
  expect(screen.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ })).not.toBeChecked();
  await user.click(screen.getByRole('checkbox', { name: /将本角色最新获准 CG 设为角色卡片图/ }));
  await user.click(screen.getByRole('checkbox', { name: '允许匹配本角色提示标签' }));
  await user.click(screen.getByRole('checkbox', { name: '允许 mature 标签' }));
  await user.click(screen.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toMatchObject({ agentConfig: { novelai: { manualEnabled: false, sceneCgEnabled: true,
    scenePromptModelConfigurationId: 'helper', autoWritebackRoleAssets: true, promptTagsEnabled: true,
    allowedPromptTagRatings: ['general', 'mature'], consentRevision: 3 } } });
  expect(screen.getByText(/辅助调用本身也计入/)).toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: '允许 explicit 标签' }));
  expect(screen.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ })).not.toBeChecked();
  await user.click(cg);
  expect(screen.queryByRole('checkbox', { name: /将本角色最新获准 CG 设为角色卡片图/ })).not.toBeInTheDocument();
});

it('shows auxiliary-only CG history without parameters, its cost and exact-attempt stop', async () => {
  const pending = { generationId: 'g', attemptId: 'a', roleId: 'r1', purpose: 'scene_cg', phase: 'auxiliary',
    status: 'prepared', parameters: null, attachments: [], taskId: 't', createdAt: '2026-10-16T04:00:00Z',
    errorCode: '', consumption: 'not_dispatched', auxiliaryConsumption: 'possible', auxiliaryStatus: 'dispatching',
    writebackStatus: 'not_requested', deliveryStatus: 'history_only', parentAttemptId: null,
    deliveryMode: 'history_only', deliveryMessageId: null, expectedAttachmentId: null };
  const { calls } = fakeBackend({ 'GET /api/services/novelai': () => ({ body: service }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [pending], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: 1, manualAttempts: 0, cgAttempts: 1, roleAssetBytes: 0 } } }),
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [], revision: 0, nextCursor: null } }),
    'POST /api/roles/r1/generations/g/cancel': () => ({ body: { generation: { ...pending,
      status: 'completed_without_image', phase: 'completed', errorCode: 'scene_none', auxiliaryStatus: 'succeeded' } } }) });
  const user = userEvent.setup();
  render(<GenerationDrawer role={role({ id: 'r1', name: '诗织' })} onClose={() => {}} onGenerated={() => {}} />);
  const record = await screen.findByRole('article', { name: '生成记录 a' });
  expect(within(record).getByText(/辅助请求已出网，可能计费/)).toBeInTheDocument();
  expect(within(record).getByText(/尚无生图参数/)).toBeInTheDocument();
  expect(screen.getByText(/本角色 CG 尝试 1\/3/)).toBeInTheDocument();
  expect(within(record).queryByRole('button', { name: /重新生成/ })).not.toBeInTheDocument();
  await user.click(within(record).getByRole('button', { name: '停止此生成' }));
  expect(await within(record).findByText('当前没有需要生成图片的场景。')).toBeInTheDocument();
  expect(calls.find((call) => call.path.endsWith('/cancel'))?.body).toEqual({ attemptId: 'a' });
  expect(within(record).queryByText(/Seed/)).not.toBeInTheDocument();
});

it('keeps a missing or unavailable explicit auxiliary connection invalid after dismissing a load error', async () => {
  let unavailable = false;
  fakeBackend({ 'GET /api/services/novelai': () => ({ body: service }),
    'GET /api/model/configurations': () => unavailable ? { status: 503, body: { detail: '辅助连接暂时不可读取' } }
      : { body: { configurations: models, activeId: 'helper' } } });
  let saved = false;
  function Harness() {
    const [draft, setDraft] = useState(draftFromRole(role({ id: 'r1', name: '诗织',
      agentConfig: { novelai: { sceneCgEnabled: true, scenePromptModelConfigurationId: 'helper', consentRevision: 3 } } as AgentToolConfig })));
    return <form onSubmit={(event) => { event.preventDefault(); saved = true; }}>
      <NovelaiRoleSettings draft={draft} onChange={setDraft} /><button>保存角色</button></form>;
  }
  const user = userEvent.setup(); render(<Harness />);
  await screen.findByRole('option', { name: /scene-observer/ });
  unavailable = true;
  await user.click(screen.getByRole('button', { name: '刷新 CG 辅助连接' }));
  const notice = await screen.findByRole('alert');
  expect(notice).toHaveTextContent('辅助连接暂时不可读取');
  await user.click(within(notice).getByRole('button', { name: '关闭提示' }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toBe(false);
  expect(screen.getByRole('combobox', { name: 'CG 辅助模型连接' })).toBeInvalid();
});

it('saves and reopens CG permission in the actual app and refreshes a late CG into its original chat and history', async () => {
  let saved = role({ id: 'r1', name: '场景角色' });
  let complete = false;
  const image = { id: 'cg-image', name: 'scene-cg.png', kind: 'image', mediaType: 'image/png', size: 100 };
  const finished = { generationId: 'cg', attemptId: 'cg-attempt', roleId: 'r1', purpose: 'scene_cg', phase: 'completed',
    status: 'succeeded', parameters: { prompt: 'rain', mode: 'txt2img', width: 1024, height: 1024, seed: 173, model: 'nai', steps: 28 },
    attachments: [image], taskId: 'cg-task', createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible',
    auxiliaryConsumption: 'possible', auxiliaryStatus: 'succeeded', writebackStatus: 'uncertain', deliveryStatus: 'sent',
    parentAttemptId: null, deliveryMode: 'source_completion', deliveryMessageId: 'cg-message', expectedAttachmentId: null };
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1': () => ({ body: { role: saved } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: complete ? [
      message({ id: 'cg-message', role: 'assistant', content: '雨中的场景', messageType: 'internal_completion',
        metadata: { attachments: [image], generationAttemptIds: ['cg-attempt'] } })] : [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models, activeId: 'helper' } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    'GET /api/roles/r1/tools': () => ({ body: { tools: [], plugins: [] } }),
    'GET /api/services/novelai': () => ({ body: service }),
    'PUT /api/roles/r1': (request) => { saved = { ...saved, ...JSON.parse(String(request?.body)) }; return { body: { role: saved } }; },
    'GET /api/roles/r1/generations': () => ({ body: { generations: [finished], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: 1, manualAttempts: 0, cgAttempts: 1, roleAssetBytes: 100 } } }),
    'GET /api/roles/r1/generations/attachment/cg-image\\?messageId=cg-message': () => ({ body: { generation: finished } }),
  });
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole('heading', { name: '场景角色', level: 1 });
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  const editor = within(await screen.findByRole('dialog', { name: '设置' }));
  await editor.findByText(/账户每日最多 20 次/);
  await user.click(editor.getByRole('checkbox', { name: '允许主人回合场景 CG' }));
  await user.selectOptions(editor.getByRole('combobox', { name: 'CG 辅助模型连接' }), 'helper');
  await user.click(editor.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ }));
  await user.click(editor.getByRole('button', { name: '保存角色参数' }));
  await waitFor(() => expect((saved.agentConfig as AgentToolConfig & { novelai?: NovelaiRoleConfig }).novelai)
    .toMatchObject({ manualEnabled: false, sceneCgEnabled: true, scenePromptModelConfigurationId: 'helper', consentRevision: 3 }));
  await user.click(editor.getByRole('button', { name: '关闭' }));
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  await screen.findByRole('combobox', { name: 'CG 辅助模型连接' });
  expect(screen.getByRole('checkbox', { name: '允许主人回合场景 CG' })).toBeChecked();
  expect(screen.getByRole('combobox', { name: 'CG 辅助模型连接' })).toHaveValue('helper');
  await user.keyboard('{Escape}');
  complete = true; fireEvent.focus(window);
  expect(await screen.findByAltText('scene-cg.png')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '更多' }));
  await user.click(await screen.findByRole('menuitem', { name: '生图与历史' }));
  const record = await screen.findByRole('article', { name: '生成记录 cg-attempt' });
  expect(within(record).getByText('卡片图写回结果尚未确认，生成图已保留历史。')).toBeInTheDocument();
  expect(within(record).getByRole('button', { name: '重新生成 scene-cg.png' })).toBeDisabled();
  expect(calls.filter((call) => call.method !== 'GET').map((call) => call.path)).toEqual(['/api/roles/r1']);
});
