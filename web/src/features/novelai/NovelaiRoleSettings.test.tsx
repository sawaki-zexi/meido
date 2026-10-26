import { useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { defaultNovelaiRoleConfig } from '../../api/novelai';
import { draftFromRole, payloadFromDraft } from '../role-management/draft';
import { NovelaiRoleSettings } from './NovelaiRoleSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('uses the plugin for chat images and preserves unrelated role policy without another consent gate', async () => {
  fakeBackend({ 'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: true,
    credentialCleanupPending: false, revision: 3, dailyAttemptLimit: 20, maxPixels: 1048576, maxSteps: 28 } }) });
  let saved: unknown;
  function Harness() {
    const [draft, setDraft] = useState(draftFromRole(role({ id: 'r1', name: '角色', agentConfig: { enabledTools: ['read_file'] } })));
    return <form onSubmit={(event) => { event.preventDefault(); saved = payloadFromDraft(draft); }}>
      <NovelaiRoleSettings draft={draft} onChange={setDraft} />
      <button type="submit">保存角色</button>
    </form>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  await screen.findByText(/账户每日最多 20 次/);
  expect(screen.queryByRole('checkbox', { name: '允许本角色在聊天中自主生成表情' })).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: /我确认当前设置的有限生图消费/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: '允许匹配本角色提示标签' }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toMatchObject({ agentConfig: { enabledTools: ['read_file'], novelai: { promptTagsEnabled: true,
    sceneCgEnabled: false, autoWritebackRoleAssets: false, consentRevision: 0 } } });
});

it('saves a bounded role-specific CG prompt instruction without repeated consent', async () => {
  const configuration = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    dailyAttemptLimit: 20, maxPixels: 1048576, maxSteps: 28 };
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/model/configurations': () => ({ body: { configurations: [{ id: 'cg-model', providerId: 'custom',
      provider: 'fixture', model: 'scene-model', baseUrl: 'http://127.0.0.1:1/v1', apiKeyConfigured: true }] } }),
  });
  let saved: unknown;
  const startingRole = role({ id: 'r1', name: '角色', agentConfig: { novelai: { ...defaultNovelaiRoleConfig,
    sceneCgEnabled: true, sceneCgDailyAttempts: 3, scenePromptModelConfigurationId: 'cg-model',
    sceneCgPromptInstructions: '', consentRevision: 3 } } });
  function Harness() {
    const [draft, setDraft] = useState(draftFromRole(startingRole));
    return <form onSubmit={(event) => { event.preventDefault(); saved = payloadFromDraft(draft); }}>
      <NovelaiRoleSettings draft={draft} onChange={setDraft} />
      <button type="submit">保存角色</button>
    </form>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  const instruction = await screen.findByLabelText('角色 CG 补充要求');
  await user.type(instruction, 'Warm lighting and a wide composition.');
  expect(screen.queryByRole('checkbox', { name: /我确认当前设置的有限生图消费/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toMatchObject({ agentConfig: { novelai: {
    sceneCgPromptInstructions: 'Warm lighting and a wide composition.', consentRevision: 0,
  } } });
  expect(instruction).toHaveAttribute('maxlength', '2000');
  await user.clear(instruction);
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toMatchObject({ agentConfig: { novelai: { sceneCgPromptInstructions: '' } } });
});
