import { useState } from 'react';
import type { Role } from '../../api/types';
import { RolePurposeSettings } from './RolePurposeSettings';
import { useAdvancedDraft } from './useAdvancedDraft';
import { api, errorMessage } from '../../api/client';

const groups = [
  { title: '对话', fields: [
    ['maxTokens', '单次回复长度上限', '回复 token 上限；同时为输入预算预留输出空间。', 1, 131072],
    ['maxIterations', '单次任务最多步数', '包括普通模型请求与上下文压缩请求。', 1, 100],
  ] },
  { title: '能力', fields: [
    ['searchEnabled', '按需工具发现', 'Shiori 的“联网搜索”实际控制工具 schema 按需加载；关闭后直接展示已授权工具。'],
    ['spawnEnabled', '创建子任务', '暂停新建和接续执行；仍可查询、取消已有任务。'],
  ] },
  { title: '记忆整理', fields: [
    ['memoryWindow', '记忆整理消息基数', '向上对齐 4 后取一半作为最近记忆保留数。', 12, 2000],
    ['memoryOptimizerEnabled', '后台整理记忆', '同时受记忆总开关控制；保存不会立即调用模型。'],
    ['memoryOptimizerIntervalSeconds', '整理间隔（秒）', '下一次正常整理的间隔，默认 18 小时。', 60, 604800],
  ] },
  { title: '上下文压缩', fields: [
    ['summaryTokenLimit', '工作摘要长度上限', '摘要 JSON 的估算 token 上限，不超过回复上限。', 1, 131072],
    ['compactionRetainedTurns', '压缩后保留原文轮数', '保留完整用户轮和当前工具链。', 0, 100],
    ['contextTriggerRatio', '上下文压缩触发比例', '按模型完整容量计算，必须大于目标比例。', .01, .99, .01],
    ['contextTargetRatio', '上下文压缩目标比例', '完整必留块可能超过目标，但不能超过可用输入容量。', .01, .99, .01],
    ['contextSafetyMarginTokens', '输入安全余量', '容量还会扣除当前请求的输出预留；token 为估算。', 0, 131072],
  ] },
  { title: '开发者', fields: [
    ['devMode', '开发者模式', '记录有界预算诊断；不保存完整请求、原图或密钥。'],
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
    <p>自动压缩需要在模型详情填写上下文容量。未填写时使用有界原文窗口，自动压缩未启用。</p>
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
      <div className="settings-preference"><span><strong>角色执行预算与工具诊断</strong><small>调整 Shell、子任务预算和受限命令许可，查看实际可用工具。</small></span>
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起' : '设置'}</button>
      </div>
      {open && <RolePurposeSettings purpose="advanced" onRoleSaved={onRoleSaved} />}
    </div></section>
  </section>;
}
