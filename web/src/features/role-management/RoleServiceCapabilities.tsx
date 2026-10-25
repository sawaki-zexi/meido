import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import { plugins, type PluginState } from '../../api/plugins';
import { getCodingConfiguration } from '../../api/coding';
import { Icon, IconButton, type IconName } from '../../ui/Icon';
import type { SettingsSectionId } from '../settings/navigation';

type OwnerStatus = { enabled: boolean; connected: boolean };
type MemoryStatus = { enabled: boolean; embeddingEnabled: boolean };
export type RoleService = { id: string; title: string; icon: IconName; status: string; description: string; section: SettingsSectionId; error?: string };

export function useRoleServices(version: number) {
  const [owner, setOwner] = useState<RoleService>({ id: 'owner', title: '主人资料', icon: 'knowledge', status: '正在读取…', description: '所有角色共享已授权的主人资料。', section: 'owner-knowledge' });
  const [services, setServices] = useState<RoleService[]>([]);
  const [host, setHost] = useState<PluginState[]>([]);
  useEffect(() => {
    let current = true;
    void Promise.allSettled([
      api<OwnerStatus>('/api/owner-knowledge'), api<MemoryStatus>('/api/memory/settings'), plugins.list(), getCodingConfiguration(),
    ]).then(([knowledge, memory, pluginData, coding]) => {
      if (!current) return;
      setOwner(previous => ({ ...previous,
        status: knowledge.status === 'rejected' ? '读取失败' : !knowledge.value.connected ? '未连接' : knowledge.value.enabled ? '已启用' : '已暂停',
        error: knowledge.status === 'rejected' ? errorMessage(knowledge.reason, '无法读取主人资料状态') : undefined,
      }));
      const hosts = pluginData.status === 'fulfilled' ? pluginData.value.plugins : [];
      setHost(hosts);
      const tools: { id: string; title: string; icon: IconName; description: string }[] = [
        { id: 'browser', title: '浏览器', icon: 'search', description: '按需浏览网页，角色各自保留浏览器会话。' },
        { id: 'screen', title: '屏幕感知', icon: 'eye', description: '在桌面对话中理解当前屏幕，需要可用的视觉模型。' },
        { id: 'computer', title: '桌面控制', icon: 'monitor', description: '按主人的指令观察和操作本机窗口。' },
      ];
      setServices([
        { id: 'memory', title: '长期记忆', icon: 'memory', section: 'memory',
          status: memory.status === 'rejected' ? '读取失败' : memory.value.enabled ? '已启用' : '已暂停',
          description: '维护与检索角色记忆，内容按角色隔离。', error: memory.status === 'rejected' ? errorMessage(memory.reason, '无法读取记忆状态') : undefined },
        ...tools.map(tool => {
          const state = hosts.find(item => item.id === tool.id);
          return { ...tool, section: 'plugins' as const, status: pluginData.status === 'rejected' ? '读取失败' : !state ? '未就绪' : state.cleanupPending ? '等待资源收尾' : state.enabled ? '已启用' : '已关闭',
            error: pluginData.status === 'rejected' ? errorMessage(pluginData.reason, '无法读取插件状态') : undefined };
        }),
        { id: 'coding', title: '编程委托', icon: 'code', section: 'coding',
          status: coding.status === 'rejected' ? '读取失败' : coding.value.enabled ? '服务已启用' : '已关闭',
          description: '在已授权的仓库内执行任务，仍需配置角色用途参数。', error: coding.status === 'rejected' ? errorMessage(coding.reason, '无法读取编程服务状态') : undefined },
      ]);
    });
    return () => { current = false; };
  }, [version]);
  return { owner, services, host };
}

export function RoleServiceCard({ service, onOpenSettings, disabled }: { service: RoleService; onOpenSettings?: (section?: SettingsSectionId) => void; disabled?: boolean }) {
  return <div className="role-runtime-card role-service-card">
    <span className="role-runtime-icon"><Icon name={service.icon} size={23} /></span>
    <div className="role-runtime-copy"><h3>{service.title}</h3><span className="role-runtime-status">{service.status}</span>
      <small className="role-runtime-usage">{service.error || service.description}</small>
    </div>
    <IconButton icon="settings" label={`${service.title}设置`} disabled={disabled || !onOpenSettings} onClick={() => onOpenSettings?.(service.section)} />
  </div>;
}
