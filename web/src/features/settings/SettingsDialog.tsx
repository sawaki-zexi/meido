import { useState, type ReactNode } from "react";
import { Dialog } from "../../ui/Dialog";
import { Icon, type IconName } from "../../ui/Icon";
import { ModelSettings } from "../model/ModelSettings";
import { OwnerKnowledgeSettings } from "./OwnerKnowledgeSettings";
import { TodoSettings } from "../todo/TodoSettings";
import { MemorySettings } from "./MemorySettings";
import { MailSettings } from "./MailSettings";
import { ChannelSettings } from "../channels/ChannelSettings";
import { IdentitySettings } from "../channels/IdentitySettings";
import { McpSettings } from "./McpSettings";
import { NovelaiSettings } from "../novelai/NovelaiSettings";
import { CodingSettings } from "../coding/CodingSettings";
import { PluginSettings } from './PluginSettings';
import type { Role } from '../../api/types';

/** Settings sections. Add an entry here to add a new section to the dialog. */
export type SettingsSectionId = "model" | "memory" | "owner-knowledge" | "todo" | "mail" | "channels" | "identity" | "mcp" | "novelai" | "coding" | "plugins";
const sections: { id: SettingsSectionId; label: string; icon: IconName; render: () => ReactNode }[] = [
  { id: "model", label: "模型", icon: "model", render: () => <ModelSettings /> },
  { id: 'plugins', label: '插件', icon: 'plug', render: () => <PluginSettings /> },
  { id: "memory", label: "记忆", icon: "memory", render: () => <MemorySettings /> },
  { id: "owner-knowledge", label: "主人资料", icon: "knowledge", render: () => <OwnerKnowledgeSettings /> },
  { id: "todo", label: "待办", icon: "todo", render: () => <TodoSettings /> },
  { id: "mail", label: "邮件", icon: "mail", render: () => <MailSettings /> },
  { id: "channels", label: "多端连接", icon: "devices", render: () => <ChannelSettings /> },
  { id: "identity", label: "我的身份", icon: "identity", render: () => <IdentitySettings /> },
  { id: "mcp", label: "MCP 服务", icon: "plug", render: () => <McpSettings /> },
  { id: "novelai", label: "NovelAI 生图", icon: "image", render: () => <NovelaiSettings /> },
  { id: "coding", label: "编程委托", icon: "code", render: () => <CodingSettings /> },
];

export function SettingsDialog({ onClose, initialSection, onRoleSaved }: { onClose: () => void; initialSection?: SettingsSectionId; onRoleSaved?: (role: Role) => void }) {
  const [current, setCurrent] = useState(initialSection ?? sections[0].id);
  const section = sections.find((item) => item.id === current) ?? sections[0];
  return <Dialog title="设置" onClose={onClose}>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">
        {sections.map((item) => <button key={item.id} type="button" className={item.id === current ? "active" : ""} aria-current={item.id === current || undefined} onClick={() => setCurrent(item.id)}>
          <Icon name={item.icon} size={18} />{item.label}
        </button>)}
      </nav>
      <div className="settings-content">{section.id === 'plugins' ? <PluginSettings onRoleSaved={onRoleSaved} /> : section.render()}</div>
    </div>
  </Dialog>;
}
