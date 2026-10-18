import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { AutonomyState } from '../../api/autonomy';
import { desktopPet, type PetState } from '../../api/desktopPet';
import { PluginRow } from './PluginRow';

export function RuntimePluginRows({ roleId, disabled, onConfigure }: {
  roleId: string; disabled: boolean; onConfigure: (id: 'autonomy' | 'pet') => void;
}) {
  const [autonomy, setAutonomy] = useState<AutonomyState | null>(null);
  const [pet, setPet] = useState<PetState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [failed, setFailed] = useState<string[]>([]);
  const path = `/api/roles/${encodeURIComponent(roleId)}`;
  useEffect(() => {
    let current = true;
    void api<AutonomyState>(`${path}/autonomy`).then(value => { if (current) setAutonomy(value); })
      .catch(() => { if (current) setFailed(items => [...items, 'autonomy']); });
    void desktopPet.status(roleId).then(value => { if (current) setPet(value); })
      .catch(() => { if (current) setFailed(items => [...items, 'pet']); });
    return () => { current = false; };
  }, [path, roleId]);
  async function toggle(id: 'autonomy' | 'pet') {
    if (disabled || busy) return;
    if (id === 'autonomy' && !(autonomy?.config.activeDecisionEnabled || autonomy?.config.driftEnabled)
      || id === 'pet' && !pet?.enabled) { onConfigure(id); return; }
    setBusy(true); setError('');
    try {
      if (id === 'autonomy') {
        const fresh = await api<AutonomyState>(`${path}/autonomy`);
        setAutonomy(await api<AutonomyState>(`${path}/autonomy`, { method: 'PUT', body: JSON.stringify({
          expectedRevision: fresh.config.revision, config: { ...fresh.config, activeDecisionEnabled: false, driftEnabled: false },
        }) }));
      } else {
        const fresh = await desktopPet.status(roleId);
        setPet(await desktopPet.configure(roleId, fresh, fresh.selectedPackageId, false));
      }
    } catch (cause) { setError(errorMessage(cause, '插件操作失败')); }
    finally { setBusy(false); }
  }
  const enabled = autonomy ? autonomy.config.activeDecisionEnabled || autonomy.config.driftEnabled : undefined;
  return <>
    {error && <p role="alert">{error}</p>}
    <PluginRow name="自主行为" description="主动决策与 Drift；模型、来源和有限许可单独配置。"
      enabled={enabled} status={autonomy ? enabled ? '已启用' : '已关闭' : failed.includes('autonomy') ? '状态读取失败' : '正在读取'}
      busy={disabled || busy} onConfigure={() => onConfigure('autonomy')} onToggle={() => void toggle('autonomy')} />
    <PluginRow name="桌宠" description="导入动作包，在桌面显示当前角色。"
      enabled={pet?.enabled} status={pet ? pet.enabled ? pet.host.status === 'ready' ? '运行中' : '已启用' : '已关闭' : failed.includes('pet') ? '状态读取失败' : '正在读取'}
      busy={disabled || busy} onConfigure={() => onConfigure('pet')} onToggle={() => void toggle('pet')} />
  </>;
}
