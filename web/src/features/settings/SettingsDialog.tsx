import { useRef, useState, type ReactNode } from 'react';
import { Dialog } from '../../ui/Dialog';
import { Icon, type IconName } from '../../ui/Icon';
import type { Role } from '../../api/types';
import { ModelSettings } from '../model/ModelSettings';
import { MemorySettings } from './MemorySettings';
import { IdentitySettings } from '../channels/IdentitySettings';
import { AppearanceSettings } from './AppearanceSettings';
import { AdvancedSettings } from './AdvancedSettings';
import { AboutSettings } from './AboutSettings';
import { PluginSettings } from './PluginSettings';
import { OwnerKnowledgeSettings } from './OwnerKnowledgeSettings';
import { TodoSettings } from '../todo/TodoSettings';
import { MailSettings } from './MailSettings';
import { ChannelSettings } from '../channels/ChannelSettings';
import { McpSettings } from './McpSettings';
import { NovelaiSettings } from '../novelai/NovelaiSettings';
import { CodingSettings } from '../coding/CodingSettings';
import { isPluginService, pluginServices, type SettingsSectionId, type SettingsServiceId } from './navigation';
export type { SettingsSectionId } from './navigation';

const sections: { id: 'model' | 'memory' | 'appearance' | 'advanced' | 'identity' | 'plugins' | 'about'; label: string; icon: IconName }[] = [
  { id: 'model', label: '模型', icon: 'model' },
  { id: 'memory', label: '记忆', icon: 'memory' },
  { id: 'appearance', label: '外观', icon: 'palette' },
  { id: 'advanced', label: '高级', icon: 'sliders' },
  { id: 'identity', label: '我的身份', icon: 'identity' },
  { id: 'plugins', label: '插件', icon: 'plug' },
  { id: 'about', label: '关于', icon: 'info' },
];
const services: Record<SettingsServiceId, () => ReactNode> = {
  'owner-knowledge': () => <OwnerKnowledgeSettings />, todo: () => <TodoSettings />, mail: () => <MailSettings />,
  channels: () => <ChannelSettings />, mcp: () => <McpSettings />, novelai: () => <NovelaiSettings />, coding: () => <CodingSettings />,
};
export function SettingsDialog({ onClose, initialSection = 'model', onRoleSaved }: {
  onClose: () => void; initialSection?: SettingsSectionId; onRoleSaved?: (role: Role) => void;
}) {
  const content = useRef<HTMLDivElement>(null);
  const serviceNeedsRefresh = useRef(isPluginService(initialSection));
  const [current, setCurrent] = useState<SettingsSectionId>(initialSection);
  const [visited, setVisited] = useState(() => new Set<SettingsSectionId>([initialSection]));
  const [pluginsVisited, setPluginsVisited] = useState(initialSection === 'plugins');
  const [serviceVersion, setServiceVersion] = useState(0);
  const service = isPluginService(current) ? current : null;
  const select = (id: SettingsSectionId) => {
    if (isPluginService(id)) serviceNeedsRefresh.current = true;
    if (id === 'plugins') {
      setPluginsVisited(true);
      if (serviceNeedsRefresh.current) {
        setServiceVersion(version => version + 1);
        serviceNeedsRefresh.current = false;
      }
    }
    setCurrent(id);
    setVisited(items => new Set([...items, id]));
    if (content.current) content.current.scrollTop = 0;
  };
  return <Dialog title="设置" variant="settings" onClose={onClose}>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">{sections.map(item =>
        <button key={item.id} type="button" className={item.id === (service ? 'plugins' : current) ? 'active' : ''}
          aria-current={item.id === (service ? 'plugins' : current) ? 'page' : undefined} onClick={() => select(item.id)}>
          <Icon name={item.icon} size={18} /><span>{item.label}</span>
        </button>)}</nav>
      <div className="settings-content" ref={content}>
        {pluginsVisited && <div hidden={current !== 'plugins'}>
          <PluginSettings onRoleSaved={onRoleSaved} onOpenService={select} serviceVersion={serviceVersion} />
        </div>}
        {visited.has('model') && <div hidden={current !== 'model'}><ModelSettings active={current === 'model'} /></div>}
        {visited.has('memory') && <div hidden={current !== 'memory'}><MemorySettings active={current === 'memory'} /></div>}
        {current === 'appearance' && <AppearanceSettings />}
        {visited.has('advanced') && <div hidden={current !== 'advanced'}><AdvancedSettings onRoleSaved={onRoleSaved} /></div>}
        {current === 'identity' && <IdentitySettings />}
        {current === 'about' && <AboutSettings />}
        {service && <section className="settings-service-page" key={service}>
          <nav className="settings-breadcrumb" aria-label="设置路径"><button type="button" className="ghost" onClick={() => select('plugins')}>
            <Icon name="back" size={16} />返回插件</button><span>{pluginServices.find(item => item.id === service)?.label}</span></nav>
          {services[service]()}
        </section>}
      </div>
    </div>
  </Dialog>;
}
