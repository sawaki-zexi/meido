import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { RoleAccountsPanel } from './RoleAccountsPanel';
import type { Account } from './channelAccounts';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('配置 Grok 后显示本机状态、交流上限与不含密钥的 Routine', async () => {
  let account: Account | null = null;
  const { calls } = fakeBackend({
    'GET /api/channels/settings': () => ({ body: { channels: [{ channel: 'grok_bot', name: 'Grok Bot', enabled: true }] } }),
    'GET /api/roles/r1/channels/accounts': () => ({ body: { accounts: account ? [account] : [] } }),
    'PUT /api/roles/r1/channels/grok_bot': () => {
      account = { accountId: 'grok-1', channel: 'grok_bot', enabled: false, allowPrivate: true,
        allowGroup: false, blockedSenders: [], displayName: 'Grok Bot', platformId: null,
        lastError: null, tokenConfigured: true, credentialsConfigured: true, maxMessages: 12,
        endpoint: 'https://example.invalid/bot', routine: '使用 D:\\Meido\\client.ps1 回程' };
      return { body: { account } };
    },
    'POST /api/roles/r1/channels/accounts/grok-1/state': () => {
      account = { ...account!, enabled: true, connectionStatus: 'connected' };
      return { body: { account } };
    },
  });
  const user = userEvent.setup();
  render(<RoleAccountsPanel roleId="r1" />);
  await user.click(await screen.findByRole('button', { name: /Grok Bot.*未连接/ }));
  await user.type(screen.getByLabelText('Grok Webhook 地址'), 'https://example.invalid/bot');
  await user.type(screen.getByLabelText('Webhook 密钥'), 'private-webhook');
  expect(screen.getByLabelText('单次交流消息上限')).toHaveValue(12);
  await user.click(screen.getByRole('button', { name: '保存并连接' }));
  expect(await screen.findByLabelText('Routine 配置')).toHaveValue('使用 D:\\Meido\\client.ps1 回程');
  expect(screen.getAllByText('本机入口已启用').length).toBeGreaterThan(0);
  expect(screen.getByLabelText('Webhook 密钥')).toHaveValue('');
  expect(calls.find(call => call.method === 'PUT')?.body).toMatchObject({ webhookKey: 'private-webhook', maxMessages: 12 });
  expect(calls.filter(call => call.path.endsWith('/test'))).toHaveLength(0);
});
