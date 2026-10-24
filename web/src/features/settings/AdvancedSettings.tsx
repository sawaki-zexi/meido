import { useState } from 'react';
import type { Role } from '../../api/types';
import { RolePurposeSettings } from './RolePurposeSettings';
import { useAdvancedDraft } from './useAdvancedDraft';
import { api, errorMessage } from '../../api/client';

const groups = [
  { title: '对话', fields: [
    ['maxTokens', '单次回复长度上限', '以 token 计，预留相应输出空间。', 1, 131072],
    ['maxIterations', '单次任务最多步数', '包含模型回复和上下文压缩。', 1, 100],
  ] },
  { title: '能力', fields: [
    ['searchEnabled', '按需工具发现', '关闭后一次加载所有已授权工具。'],
    ['spawnEnabled', '创建子任务', '关闭后仍可查询、取消已有任务。'],
  ] },
  { title: '记忆整理', fields: [
    ['memoryWindow', '记忆整理消息基数', '约一半保留为最近记忆。', 12, 2000],
    ['memoryOptimizerEnabled', '后台整理记忆', '需同时启用记忆。'],
    ['memoryOptimizerIntervalSeconds', '整理间隔（秒）', '默认 18 小时。', 60, 604800],
  ] },
  { title: '上下文压缩', fields: [
    ['summaryTokenLimit', '工作摘要长度上限', '以 token 计，不超过回复上限。', 1, 131072],
    ['compactionRetainedTurns', '压缩后保留原文轮数', '保留完整对话轮次。', 0, 100],
    ['contextTriggerRatio', '上下文压缩触发比例', '须大于目标比例。', .01, .99, .01],
    ['contextTargetRatio', '上下文压缩目标比例', '必要上下文可能超过此比例。', .01, .99, .01],
    ['contextSafetyMarginTokens', '输入安全余量', '以 token 计，额外预留输入空间。', 0, 131072],
  ] },
  { title: '开发者', fields: [
    ['devMode', '开发者模式', '记录运行诊断，不保存原图或密钥。'],
  ] },
] as const;

export function AdvancedSettings({ onRoleSaved }: { onRoleSaved?: (role: Role) => void }) {
  const [open, setOpen] = useState(false);
  const settings = useAdvancedDraft();
  const [diagnostics, setDiagnostics] = useState<Array<{ runId: string; roleId: string; requests: number; compactions: number; inputEstimate: number; toolCount: number; elapsedMs: number }>>([]);
  const [diagnosticError, setDiagnosticError] = useState('');
  async function refreshDiagnostics() {
    try {
      setDiagnostics((await api<{ diagnostics: typeof diagnostics }>('/api/settings/advanced/diagnostics')).diagnostics);
      setDiagnosticError('');
    } catch (error) { setDiagnosticError(errorMessage(error, '无法读取运行诊断')); }
  }
  return <section className="settings-domain" aria-label="高级设置">
    <h2>高级</h2>
    <p role="status">{settings.status}</p>
    {settings.problem && <div role="alert"><p>{settings.problem}</p>
      <button type="button" onClick={settings.retry}>重试保存</button>
      <button type="button" onClick={() => void settings.reload()}>放弃草稿并重新读取</button>
    </div>}
    <p>自动压缩需先填写模型的上下文容量。</p>
    {groups.map(group => <section className="settings-group" key={group.title}><h3>{group.title}</h3><div className="settings-card">
      {group.fields.map(field => <div className="settings-preference" key={field[0]}>
        <label htmlFor={`advanced-${field[0]}`}><strong>{field[1]}</strong><small>{field[2]}</small></label>
        {field.length === 3 ? <button id={`advanced-${field[0]}`} type="button" role="switch" className="settings-switch" aria-label={field[1]} disabled={!settings.loaded}
          aria-checked={settings.draft[field[0]] === true} onClick={() => settings.change(field[0], settings.draft[field[0]] !== true)}><span /></button> :
          <input className="advanced-number" id={`advanced-${field[0]}`} type="number" disabled={!settings.loaded} value={String(settings.draft[field[0]] ?? '')}
            min={field[3]} max={field[4]} step={field.length === 6 ? field[5] : 1}
            onChange={event => settings.change(field[0], event.target.value)} />}
      </div>)}
    </div></section>)}
    {settings.draft.devMode === true && <section className="settings-group"><h3>运行诊断</h3><div className="settings-card">
      <button type="button" onClick={() => void refreshDiagnostics()}>刷新运行诊断</button>
      {diagnosticError && <p role="alert">{diagnosticError}</p>}
      {diagnostics.length === 0 && <p>暂无诊断。启用并保存后，下一次普通聊天会记录预算信息。</p>}
      {diagnostics.map(item => <p key={item.runId}>{item.roleId} · 请求 {item.requests} 次 · 压缩 {item.compactions} 次 · 输入估算 {item.inputEstimate} token · 工具 {item.toolCount} 个 · {item.elapsedMs} ms</p>)}
    </div></section>}
    <section className="settings-group"><h3>运行设置</h3><div className="settings-card">
      <div className="settings-preference"><span><strong>角色执行预算与工具诊断</strong><small>Shell、子任务与工具权限。</small></span>
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起' : '设置'}</button>
      </div>
      {open && <RolePurposeSettings purpose="advanced" onRoleSaved={onRoleSaved} />}
    </div></section>
  </section>;
}
