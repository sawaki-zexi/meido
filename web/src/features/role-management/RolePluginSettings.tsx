import type { RoleDraft } from './draft';
import { ScreenPerceptionSettings } from './ScreenPerceptionSettings';
import { ComputerUseSettings } from './ComputerUseSettings';
import { NovelaiRoleSettings } from '../novelai/NovelaiRoleSettings';
import { CodingRoleSettings } from '../coding/CodingRoleSettings';
import { AutonomySettings } from './AutonomySettings';
import { DesktopPetSettings } from './DesktopPetSettings';

export function RolePluginSettings({ roleId, draft, onChange, disabled }: {
  roleId?: string; draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean;
}) {
  return <fieldset>
    <legend>插件设置</legend>
    <p className="field-hint">特殊插件按角色启用或暂停。关闭保留已有配置；基础工具不需要在这里开启。</p>
    <label className="field checkbox-field">
      <input type="checkbox" aria-label="允许查询主人待办" checked={draft.todoSearchEnabled}
        disabled={disabled} onChange={(event) => onChange({ ...draft, todoSearchEnabled: event.target.checked })} />
      <span>允许查询主人待办</span>
      <small className="field-hint">只读取与当前问题相关的事项。</small>
    </label>
    <label className="field checkbox-field">
      <input type="checkbox" aria-label="允许浏览器操作网页" checked={draft.browserUseEnabled}
        disabled={disabled} onChange={(event) => onChange({ ...draft, browserUseEnabled: event.target.checked })} />
      <span>允许浏览器操作网页</span>
      <small className="field-hint">在角色独立浏览器中访问网页和交互，需要已安装的浏览器运行组件。</small>
    </label>
    <ScreenPerceptionSettings draft={draft} onChange={onChange} disabled={disabled} />
    <ComputerUseSettings draft={draft} onChange={onChange} disabled={disabled} />
    <NovelaiRoleSettings draft={draft} onChange={onChange} disabled={disabled} />
    <CodingRoleSettings roleId={roleId} draft={draft} onChange={onChange} disabled={disabled} />
    {roleId && <AutonomySettings key={roleId} roleId={roleId} disabled={disabled} />}
    {roleId && <DesktopPetSettings key={`pet-${roleId}`} roleId={roleId} disabled={disabled} />}
  </fieldset>;
}
