import { useState } from 'react';
import type { RoleDraft } from './draft';
import { defaultNovelaiRoleConfig } from '../../api/novelai';
import { defaultCodingRole } from '../../api/coding';
import { ScreenPerceptionSettings } from './ScreenPerceptionSettings';
import { ComputerUseSettings } from './ComputerUseSettings';
import { NovelaiRoleSettings } from '../novelai/NovelaiRoleSettings';
import { CodingRoleSettings } from '../coding/CodingRoleSettings';
import { AutonomySettings } from './AutonomySettings';
import { DesktopPetSettings } from './DesktopPetSettings';
import { ShellSettings } from './ShellSettings';
import { SpawnSettings } from './SpawnSettings';
import { ToolCatalog } from './ToolCatalog';
import { PluginRow } from '../settings/PluginRow';
import { RuntimePluginRows } from '../settings/RuntimePluginRows';

type PluginId = 'todo' | 'browser' | 'screen' | 'computer' | 'novelai' | 'coding';
const plugins: { id: PluginId; name: string; description: string; enabled: (draft: RoleDraft) => boolean }[] = [
  { id: 'browser', name: '浏览器', description: '在角色独立浏览器中访问网页和交互。', enabled: draft => draft.browserUseEnabled },
  { id: 'screen', name: '屏幕感知', description: '使用指定视觉连接观察主屏并返回摘要。', enabled: draft => draft.screenPerceptionEnabled },
  { id: 'computer', name: '桌面控制', description: '窗口截图发送至当前聊天模型，并可操作键盘、鼠标。', enabled: draft => draft.computerUseEnabled },
  { id: 'novelai', name: 'NovelAI 生图', description: '关闭会撤销手动生图、CG 与卡片写回许可；保留连接、额度和标签。', enabled: draft => Boolean(draft.preservedAgentConfig.novelai?.manualEnabled || draft.preservedAgentConfig.novelai?.sceneCgEnabled) },
  { id: 'coding', name: '编程委托', description: '向可信仓库和专用 CLI 委托编程任务。', enabled: draft => Boolean(draft.preservedAgentConfig.coding?.enabled) },
  { id: 'todo', name: '待办查询', description: '读取与当前问题相关的主人待办。', enabled: draft => draft.todoSearchEnabled },
];
function toggled(draft: RoleDraft, id: PluginId, enabled: boolean): RoleDraft {
  if (id === 'todo') return { ...draft, todoSearchEnabled: enabled };
  if (id === 'browser') return { ...draft, browserUseEnabled: enabled };
  if (id === 'screen') return { ...draft, screenPerceptionEnabled: enabled };
  if (id === 'computer') return { ...draft, computerUseEnabled: enabled };
  if (id === 'coding') return { ...draft, preservedAgentConfig: { ...draft.preservedAgentConfig,
    coding: { ...defaultCodingRole(), ...draft.preservedAgentConfig.coding, enabled } } };
  return { ...draft, preservedAgentConfig: { ...draft.preservedAgentConfig,
    novelai: { ...defaultNovelaiRoleConfig, ...draft.preservedAgentConfig.novelai,
      manualEnabled: false, sceneCgEnabled: false, autoWritebackRoleAssets: false, consentRevision: 0 } } };
}
export function RolePluginSettings({ roleId, draft, savedDraft, onChange, onSave, disabled }: {
  roleId: string; draft: RoleDraft; savedDraft: RoleDraft; onChange: (draft: RoleDraft) => void;
  onSave: (draft: RoleDraft) => Promise<boolean>; disabled: boolean;
}) {
  const [active, setActive] = useState<PluginId | 'autonomy' | 'pet' | 'advanced' | null>(null);
  const configure = (id: NonNullable<typeof active>, start = false) => {
    onChange(start && id !== 'novelai' && plugins.some(plugin => plugin.id === id) ? toggled(savedDraft, id as PluginId, true) : savedDraft);
    setActive(id);
  };
  const toggle = (id: PluginId) => {
    const enabled = plugins.find(plugin => plugin.id === id)!.enabled(savedDraft);
    if (!enabled && id !== 'browser' && id !== 'computer' && id !== 'todo') { configure(id, true); return; }
    void onSave(toggled(savedDraft, id, !enabled));
  };
  if (active === null) return <>
    <div className="plugin-list" aria-label="可选插件">
      {plugins.map(plugin => <PluginRow key={plugin.id} name={plugin.name} description={plugin.description}
        enabled={plugin.enabled(savedDraft)} status={plugin.enabled(savedDraft) ? '已启用' : '已关闭'} busy={disabled}
        onConfigure={() => configure(plugin.id)} onToggle={() => toggle(plugin.id)} stopLabel={plugin.id === 'novelai' ? '关闭生成许可' : undefined} />)}
      <RuntimePluginRows key={roleId} roleId={roleId} disabled={disabled} onConfigure={configure} />
    </div>
    <button type="button" className="ghost plugin-advanced" disabled={disabled} onClick={() => configure('advanced')}>高级运行设置</button>
  </>;
  const name = plugins.find(plugin => plugin.id === active)?.name ?? { autonomy: '自主行为', pet: '桌宠', advanced: '高级运行设置' }[active as 'autonomy' | 'pet' | 'advanced'];
  const content = active === 'screen' ? <ScreenPerceptionSettings draft={draft} onChange={onChange} disabled={disabled} />
    : active === 'computer' ? <ComputerUseSettings draft={draft} onChange={onChange} disabled={disabled} />
    : active === 'novelai' ? <NovelaiRoleSettings draft={draft} onChange={onChange} disabled={disabled} />
    : active === 'coding' ? <CodingRoleSettings roleId={roleId} draft={draft} onChange={onChange} disabled={disabled} />
    : active === 'advanced' ? <><ShellSettings draft={draft} onChange={onChange} disabled={disabled} /><SpawnSettings draft={draft} onChange={onChange} disabled={disabled} /></>
    : active === 'browser' || active === 'todo' ? <label className="field checkbox-field">
      <input type="checkbox" checked={active === 'browser' ? draft.browserUseEnabled : draft.todoSearchEnabled} disabled={disabled}
        onChange={event => onChange(toggled(draft, active, event.target.checked))} />{active === 'browser' ? '允许浏览器操作网页' : '允许查询主人待办'}
    </label> : null;
  return <section className="plugin-detail" aria-label={`${name}设置`}>
    <div className="settings-subheading"><h4>{name}</h4><button type="button" className="ghost" disabled={disabled}
      onClick={() => { onChange(savedDraft); setActive(null); }}>返回插件列表</button></div>
    {active === 'autonomy' ? <AutonomySettings key={roleId} roleId={roleId} disabled={disabled} />
      : active === 'pet' ? <DesktopPetSettings key={roleId} roleId={roleId} disabled={disabled} />
      : <form onSubmit={event => { event.preventDefault(); void onSave(draft); }}>
        {content}<div className="form-actions"><button type="submit" className="primary" disabled={disabled}>{disabled ? '保存中…' : '保存'}</button></div>
      </form>}
    {active === 'advanced' && <ToolCatalog roleId={roleId} />}
  </section>;
}
