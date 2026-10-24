import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { AutonomySettings } from './AutonomySettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const config = { revision: 4, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null,
  defaultTargetRef: 'desktop', allowedTargetRefs: ['desktop'], sourceGrants: [],
  driftToolGrants: [], shellRuleIds: [], consentRevision: 0,
  driftAuthoring: { enabled: true, scopeId: 'scope-current', revision: 2, allowedTools: ['read_file'], allowMcp: true,
    allowShell: false, allowAutonomousAuthoring: false, allowedResourceIds: [] },
  driftMcpGrants: [], driftReadResourceIds: [], driftAttachmentIds: [] };
const state = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };
const hash = 'a'.repeat(64);
const preview = { previewId: 'b'.repeat(32), skillId: 'reading-notes', bundleSha256: hash,
  files: [{ path: 'reading-notes/SKILL.md', size: 100, sha256: 'c'.repeat(64) }],
  declaration: { tools: ['read_file'], requiresMCP: [{ serverId: 'notes', tools: [{ name: 'search' }] }] },
  scopeId: 'scope-current', scopeRevision: 2, configRevision: 4, status: 'untrusted' };
const pending = { id: 'reading-notes', bundleSha256: hash, status: 'pending_dependency',
  provenance: { status: 'verified', runId: null, authorSkillHash: null }, missingMcp: ['notes:search'] };

it('requires an explicit current opaque preview adoption and explains pending dependencies without regenerating the same bundle', async () => {
  let dependencyReady = false;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: state }),
    'GET /api/roles/r[12]/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/autonomy/drift-skills': () => ({ body: { skills: dependencyReady ? [{ ...pending, status: 'available', missingMcp: [],
      progress: { runCount: 2, lastRun: 1790000000, oneLine: '已整理第二章', nextAction: '继续第三章', messageResult: 'sent', projectionPending: true } }]
      : [{ id: 'reading-notes', status: 'untrusted' }] } }),
    'POST /api/roles/r1/autonomy/drift-artifact-preview': () => ({ body: preview }),
    'POST /api/roles/r1/autonomy/drift-skill-adoptions': () => ({ body: { skills: [pending] } }),
  });
  const user = userEvent.setup(); render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '查看 Drift 技能' }));
  await screen.findByRole('article', { name: 'Drift 技能 reading-notes' });
  await user.type(screen.getByLabelText('Drift 技能 ID'), 'reading-notes');
  await user.click(screen.getByRole('button', { name: '预览 Drift 技能产物' }));
  expect(await screen.findByText(`产物 SHA256：${hash}`)).toBeInTheDocument();
  expect(screen.getByText(/reading-notes\/SKILL.md/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '采用当前 Drift 产物' })).toBeDisabled();
  expect(calls.filter((call) => call.path.endsWith('drift-skill-adoptions'))).toHaveLength(0);
  await user.click(screen.getByRole('checkbox', { name: '我确认采用当前预览的精确产物及作者范围' }));
  await user.click(screen.getByRole('button', { name: '采用当前 Drift 产物' }));
  const skill = await screen.findByRole('article', { name: 'Drift 技能 reading-notes' });
  expect(await within(skill).findByText(/缺少 MCP 许可：notes:search/)).toBeInTheDocument();
  expect(within(skill).getByText('来源已核验，等待执行依赖')).toBeInTheDocument();
  expect(calls.find((call) => call.path.endsWith('drift-skill-adoptions'))?.body).toEqual({
    previewId: 'b'.repeat(32), skillId: 'reading-notes', bundleSha256: hash,
    authoringScopeId: 'scope-current', expectedScopeRevision: 2, expectedRevision: 4 });
  expect(screen.getByText(/补齐缺失许可后刷新即可，无需重新生成/)).toBeVisible();
  dependencyReady = true;
  await user.click(screen.getByRole('button', { name: '刷新 Drift 技能状态' }));
  expect(await screen.findByText('本地许可与可信产物齐备')).toBeInTheDocument();
  expect(screen.getByText(/已运行 2 次 · 已整理第二章/)).toBeInTheDocument();
  expect(screen.getByText('下一步：继续第三章')).toBeInTheDocument();
  expect(screen.getByText(/进度已持久保存，工作文件投影待恢复/)).toBeInTheDocument();
  expect(screen.getByText(/最近消息结果：已发送，有真实投递回执/)).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(2);
});

it('retains the same adoption identity after 503 and requires a new preview and confirmation after 409', async () => {
  let previews = 0; let adoptions = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: state }),
    'GET /api/roles/r[12]/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/autonomy/drift-skills': () => ({ body: { skills: [] } }),
    'POST /api/roles/r1/autonomy/drift-artifact-preview': () => ({ body: { ...preview, previewId: (++previews === 1 ? 'b' : 'd').repeat(32) } }),
    'POST /api/roles/r1/autonomy/drift-skill-adoptions': () => ++adoptions === 1
      ? { status: 503, body: { detail: '采用暂不可确认' } } : adoptions === 2
      ? { status: 409, body: { detail: '目录身份已变化，请重新预览并明确采用' } } : { body: { skills: [pending] } },
  });
  const user = userEvent.setup(); render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '查看 Drift 技能' }));
  await user.type(screen.getByLabelText('Drift 技能 ID'), 'reading-notes');
  await user.click(screen.getByRole('button', { name: '预览 Drift 技能产物' }));
  await screen.findByText(`产物 SHA256：${hash}`);
  await user.click(screen.getByRole('checkbox', { name: /我确认采用当前预览/ }));
  await user.click(screen.getByRole('button', { name: '采用当前 Drift 产物' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('采用暂不可确认');
  expect(adoptions).toBe(1); expect(previews).toBe(1);
  await user.click(screen.getByRole('checkbox', { name: /我确认采用当前预览/ }));
  await user.click(screen.getByRole('button', { name: '重查本次采用' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('目录身份已变化');
  expect(screen.queryByRole('checkbox', { name: /我确认采用当前预览/ })).not.toBeInTheDocument();
  const adoptionCalls = calls.filter((call) => call.path.endsWith('drift-skill-adoptions'));
  expect(adoptionCalls[0].body).toEqual(adoptionCalls[1].body);
  await user.click(screen.getByRole('button', { name: '预览 Drift 技能产物' }));
  expect(await screen.findByRole('button', { name: '采用当前 Drift 产物' })).toBeDisabled();
  expect(adoptions).toBe(2);
  await user.click(screen.getByRole('checkbox', { name: /我确认采用当前预览/ }));
  await user.click(screen.getByRole('button', { name: '采用当前 Drift 产物' }));
  await screen.findByText('来源已核验，等待执行依赖');
  expect((calls.filter((call) => call.path.endsWith('drift-skill-adoptions'))[2].body as { previewId: string }).previewId).toBe('d'.repeat(32));
});

it('rejects late preview responses after role A-B-A and after an unsaved author-scope off-on cycle', async () => {
  let finishPreview: ((value: { body: unknown }) => void) | undefined;
  const { calls } = fakeBackend({
    'GET /api/roles/r[12]/autonomy': () => ({ body: state }),
    'GET /api/roles/r[12]/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r[12]/autonomy/drift-skills': () => ({ body: { skills: [] } }),
    'POST /api/roles/r1/autonomy/drift-artifact-preview': () => new Promise((resolve) => { finishPreview = resolve; }),
  });
  const user = userEvent.setup(); const view = render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '查看 Drift 技能' }));
  await user.type(screen.getByLabelText('Drift 技能 ID'), 'reading-notes');
  await user.click(screen.getByRole('button', { name: '预览 Drift 技能产物' }));
  await waitFor(() => expect(finishPreview).toBeDefined());
  const old = finishPreview;
  view.rerender(<AutonomySettings roleId="r2" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  view.rerender(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '查看 Drift 技能' }));
  await act(async () => { old?.({ body: preview }); });
  expect(screen.getByLabelText('Drift 技能 ID')).toHaveValue('');
  expect(screen.queryByText(`产物 SHA256：${hash}`)).not.toBeInTheDocument();
  finishPreview = undefined;
  await user.type(screen.getByLabelText('Drift 技能 ID'), 'reading-notes');
  await user.click(screen.getByRole('button', { name: '预览 Drift 技能产物' }));
  await waitFor(() => expect(finishPreview).toBeDefined());
  await user.click(screen.getByRole('checkbox', { name: '允许创建或更新 Drift 技能' }));
  await user.click(screen.getByRole('checkbox', { name: '允许创建或更新 Drift 技能' }));
  await act(async () => { finishPreview?.({ body: preview }); });
  expect(screen.queryByRole('button', { name: '采用当前 Drift 产物' })).not.toBeInTheDocument();
  expect(calls.filter((call) => call.path.endsWith('drift-skill-adoptions'))).toHaveLength(0);
});
