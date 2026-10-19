import { useState } from 'react';
import type { Role } from '../../api/types';
import { RolePurposeSettings } from './RolePurposeSettings';

export function AdvancedSettings({ onRoleSaved }: { onRoleSaved?: (role: Role) => void }) {
  const [open, setOpen] = useState(false);
  return <section className="settings-domain" aria-label="高级设置">
    <h2>高级</h2>
    <section className="settings-group"><h3>运行设置</h3><div className="settings-card">
      <div className="settings-preference"><span><strong>角色执行预算与工具诊断</strong><small>调整 Shell、子任务预算和受限命令许可，查看实际可用工具。</small></span>
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起' : '设置'}</button>
      </div>
      {open && <RolePurposeSettings purpose="advanced" onRoleSaved={onRoleSaved} />}
    </div></section>
  </section>;
}
