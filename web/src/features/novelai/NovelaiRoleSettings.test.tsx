import { useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { draftFromRole, payloadFromDraft } from '../role-management/draft';
import { NovelaiRoleSettings } from './NovelaiRoleSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('requires explicit current-version finite consent and preserves unrelated role policy', async () => {
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
  expect(await screen.findByRole('checkbox', { name: '允许按主人输入生图' })).not.toBeChecked();
  await screen.findByText(/账户每日最多 20 次/);
  await user.click(screen.getByRole('checkbox', { name: '允许按主人输入生图' }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toBeUndefined();
  await user.clear(screen.getByLabelText('角色手动每日最多尝试'));
  await user.type(screen.getByLabelText('角色手动每日最多尝试'), '5');
  await user.click(screen.getByRole('checkbox', { name: /我确认当前设置的有限生图消费/ }));
  await user.click(screen.getByRole('button', { name: '保存角色' }));
  expect(saved).toMatchObject({ agentConfig: { enabledTools: ['read_file'], novelai: { manualEnabled: true,
    manualDailyAttempts: 5, sceneCgEnabled: false, autoWritebackRoleAssets: false, consentRevision: 3 } } });
});
