import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { autonomy, type AutonomyConfiguration, type AutonomyTarget, type AutonomyTargetAccount, type AutonomyTargetChoice,
  type AutonomyTargetDirectory, type AutonomyTargetGrant, type TargetDirectoryQuery } from '../../api/autonomy';
import { Notice, Placeholder } from '../../ui/Status';

const kindLabels: Record<TargetDirectoryQuery['kind'], string> = { known: '已观察目标', friends: '好友', groups: '群组', members: '群成员', member: '成员资料' };
const onlineLabel = (online: boolean | null | undefined) => online === true ? '在线' : online === false ? '离线' : '在线状态未知';
const eligible = (target: AutonomyTarget) => target.ownerEligible && target.kind === 'private' && target.canSend !== false && Boolean(target.selectionObservationId);

function useTargetDirectory(roleId: string, revision: number) {
  const [accounts, setAccounts] = useState<AutonomyTargetAccount[]>([]);
  const [accountsBusy, setAccountsBusy] = useState(false);
  const [accountsError, setAccountsError] = useState('');
  const [query, setQuery] = useState<TargetDirectoryQuery>({ accountId: '', kind: 'known', limit: 20 });
  const [page, setPage] = useState<AutonomyTargetDirectory | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const directoryRequest = useRef(0);
  const accountsRequest = useRef(0);

  async function reloadAccounts() {
    const scope = generation.current, current = ++accountsRequest.current;
    setAccountsBusy(true); setAccountsError('');
    try {
      const value = await autonomy.targetAccounts(roleId);
      if (scope === generation.current && current === accountsRequest.current) setAccounts(value.accounts);
    } catch (cause) {
      if (scope === generation.current && current === accountsRequest.current) setAccountsError(errorMessage(cause, '账号连接事实暂不可读'));
    } finally { if (scope === generation.current && current === accountsRequest.current) setAccountsBusy(false); }
  }
  useEffect(() => {
    ++generation.current; ++directoryRequest.current;
    setAccounts([]); setPage(null); setQuery({ accountId: '', kind: 'known', limit: 20 }); setBusy(false); setError('');
    void reloadAccounts();
    return () => { ++generation.current; ++directoryRequest.current; ++accountsRequest.current; };
  }, [roleId, revision]);

  function changeQuery(value: TargetDirectoryQuery) {
    ++directoryRequest.current; setBusy(false); setPage(null); setError(''); setQuery(value);
  }
  async function read(next = false) {
    if (!query.accountId || busy || next && !page?.nextCursor) return;
    const scope = generation.current, current = ++directoryRequest.current;
    const request = { ...query, ...(next && page?.nextCursor ? { cursor: page.nextCursor } : {}) };
    setBusy(true); setError('');
    try {
      const value = await autonomy.targetDirectory(roleId, request);
      if (scope !== generation.current || current !== directoryRequest.current) return;
      setPage({ ...value, targets: next ? [...(page?.targets ?? []), ...value.targets.filter((target) => !page?.targets.some((old) => old.id === target.id))] : value.targets });
    } catch (cause) {
      if (scope === generation.current && current === directoryRequest.current) {
        setPage(null); setError(errorMessage(cause, '目标目录读取失败；已保存目标仍保留'));
      }
    } finally { if (scope === generation.current && current === directoryRequest.current) setBusy(false); }
  }
  return { accounts, accountsBusy, accountsError, reloadAccounts, query, changeQuery, page, busy, error, read };
}

export function AutonomyTargets({ roleId, revision, config, grants, choices, onChange }: {
  roleId: string; revision: number; config: AutonomyConfiguration; grants: AutonomyTargetGrant[]; choices: AutonomyTargetChoice[];
  onChange: (changes: Partial<AutonomyConfiguration>, choices: AutonomyTargetChoice[]) => void;
}) {
  const directory = useTargetDirectory(roleId, revision);
  const account = directory.accounts.find((item) => item.accountId === directory.query.accountId);
  const label = (ref: string) => ref === 'desktop' ? '当前角色桌面会话'
    : choices.find((item) => item.ref === ref)?.displayName ?? grants.find((item) => item.ref === ref)?.displayName ?? '原目标（当前目录不可用）';
  function select(ref: string, checked: boolean, choice?: AutonomyTargetChoice) {
    const refs = checked ? [...new Set([...config.allowedTargetRefs, ref])] : config.allowedTargetRefs.filter((item) => item !== ref);
    if (refs.length > 4) return;
    const remaining = choices.filter((item) => item.ref !== ref && refs.includes(item.ref));
    onChange({ allowedTargetRefs: refs, defaultTargetRef: refs.includes(config.defaultTargetRef) ? config.defaultTargetRef : '', consentRevision: 0 },
      checked && choice ? [...remaining, choice] : remaining);
  }
  function selectObserved(target: AutonomyTarget) {
    if (!directory.page || !eligible(target)) return;
    select(target.targetRef, true, { ref: target.targetRef, displayName: target.displayName,
      selection: { accountId: target.accountId, targetId: target.id, selectionObservationId: target.selectionObservationId!,
        expectedDirectoryRevision: directory.page.directoryRevision } });
  }
  return <fieldset aria-label="自主发送目标">
    <legend>自主发送目标</legend>
    <p>最多许可4个目标（含桌面），默认目标必须在许可集合中。外部仅限当前角色账号已绑定的主人私聊；保存不会发送消息或运行来源。</p>
    <label className="field checkbox-field"><input type="checkbox" aria-label="许可目标 当前角色桌面会话"
      checked={config.allowedTargetRefs.includes('desktop')} disabled={!config.allowedTargetRefs.includes('desktop') && config.allowedTargetRefs.length >= 4}
      onChange={(event) => select('desktop', event.target.checked)} />当前角色桌面会话</label>
    {config.allowedTargetRefs.filter((ref) => ref !== 'desktop').map((ref) => {
      const grant = grants.find((item) => item.ref === ref);
      const pending = choices.some((item) => item.ref === ref);
      return <div key={ref}>
        <label className="field checkbox-field"><input type="checkbox" aria-label={`保留目标 ${label(ref)}`} checked
          onChange={() => select(ref, false)} />{pending ? '待保存目标' : '已保存目标'}：{label(ref)}</label>
        {grant && <p>{grant.accountDisplayName ?? grant.accountId} · {grant.channel} · {onlineLabel(grant.online)}
          {grant.connection ? ` · 连接：${grant.connection}` : ''}{grant.observedAt ? ` · 观察时间：${grant.observedAt}` : ''}</p>}
        {!pending && (!grant || !grant.current) && <p role="status">目标许可已失效：{grant?.denial || '当前目标事实不可用'}；请重新读取目录并明确重新选择或移除。</p>}
        {grant?.reason && <p>{grant.reason}</p>}
      </div>;
    })}
    <label className="field"><span>自主默认目标</span><select aria-label="自主默认目标" value={config.defaultTargetRef}
      onChange={(event) => onChange({ defaultTargetRef: event.target.value, consentRevision: 0 }, choices)}>
      <option value="">请选择许可集合中的默认目标</option>
      {config.allowedTargetRefs.map((ref) => <option key={ref} value={ref}>{label(ref)}</option>)}
    </select></label>
    <p>当前已选择 {config.allowedTargetRefs.length}/4 个目标。</p>
    {directory.accountsBusy && <Placeholder loading>正在读取本地账号连接事实…</Placeholder>}
    {directory.accountsError && <Notice>{directory.accountsError}</Notice>}
    <button type="button" disabled={directory.accountsBusy} onClick={() => void directory.reloadAccounts()}>刷新自主发送账号</button>
    <label className="field"><span>自主发送账号</span><select aria-label="自主发送账号" value={directory.query.accountId}
      onChange={(event) => {
        const selected = directory.accounts.find((item) => item.accountId === event.target.value);
        directory.changeQuery({ accountId: event.target.value, kind: selected?.supportedKinds.includes('known') ? 'known' : selected?.supportedKinds[0] ?? 'known', limit: 20 });
      }}><option value="">请选择当前角色的渠道账号</option>
      {directory.accounts.map((item) => <option key={item.accountId} value={item.accountId}>{item.displayName} · {item.channel}</option>)}
    </select></label>
    {account && <>
      <p>{onlineLabel(account.online)}</p><p>连接：{account.connection} · 账号{account.enabled ? '启用' : '停用'} · 平台{account.platformEnabled ? '启用' : '停用'} · 当前{account.effectiveEnabled ? '有效启用' : '未启用'}</p>
      <p>观察时间：{account.observedAt || '尚无观察时间'}{account.reason ? ` · ${account.reason}` : ''}</p>
      <p>支持目录：{account.supportedKinds.map((kind) => kindLabels[kind]).join('、') || '无'}；连接状态与已观察目录不能单独证明平台在线。</p>
      <p>媒体能力：{account.capabilities.media === true ? '支持' : account.capabilities.media === false ? '不支持' : '尚无能力事实'}；实际发送仍由当前平台能力校验。</p>
      <label className="field"><span>目标目录类型</span><select aria-label="目标目录类型" value={directory.query.kind}
        onChange={(event) => directory.changeQuery({ accountId: account.accountId, kind: event.target.value as TargetDirectoryQuery['kind'], limit: 20 })}>
        {account.supportedKinds.map((kind) => <option key={kind} value={kind}>{kindLabels[kind]}</option>)}
      </select></label>
      {(directory.query.kind === 'members' || directory.query.kind === 'member') && <label className="field"><span>目录群 ID（仅查询）</span>
        <input aria-label="目录群 ID（仅查询）" value={directory.query.groupId ?? ''} onChange={(event) => directory.changeQuery({ ...directory.query, groupId: event.target.value })} /></label>}
      {directory.query.kind === 'member' && <label className="field"><span>目录成员 ID（仅查询）</span>
        <input aria-label="目录成员 ID（仅查询）" value={directory.query.memberId ?? ''} onChange={(event) => directory.changeQuery({ ...directory.query, memberId: event.target.value })} /></label>}
    </>}
    <button type="button" disabled={!account || !account.supportedKinds.length || directory.busy}
      onClick={() => void directory.read()}>读取目标目录</button>
    {directory.busy && <Placeholder loading>正在读取所选目录…</Placeholder>}
    {directory.error && <Notice>{directory.error}；已保存目标仍保留，请核对后重新读取。</Notice>}
    {directory.page && <section aria-label="自主目标目录">
      <p>覆盖范围：{directory.page.coverage === 'known' ? '已观察目标（非完整平台目录）' : '平台目录'} · {directory.page.complete ? '本次目录已完整' : '本次目录尚未完整'} · 观察时间：{directory.page.observedAt} · 总数：{directory.page.total}</p>
      {directory.page.targets.length === 0 && <p>本次目录没有目标。</p>}
      {directory.page.targets.map((target) => {
        const selected = config.allowedTargetRefs.includes(target.targetRef);
        const selectable = eligible(target);
        return <div key={target.id}><label className="field checkbox-field"><input type="checkbox" aria-label={`许可目标 ${target.displayName}`}
          checked={selected} disabled={!selected && (!selectable || config.allowedTargetRefs.length >= 4)}
          onChange={(event) => event.target.checked ? selectObserved(target) : select(target.targetRef, false)} />{target.displayName} · {target.kind}</label>
          {!selectable && <p>{target.reason || '不是可发送的已绑定主人私聊'}</p>}
          {selected && selectable && <button type="button" onClick={() => selectObserved(target)}>重新选择目标 {target.displayName}</button>}
        </div>;
      })}
      {directory.page.nextCursor && <button type="button" disabled={directory.busy} onClick={() => void directory.read(true)}>读取下一页目标</button>}
    </section>}
  </fieldset>;
}
