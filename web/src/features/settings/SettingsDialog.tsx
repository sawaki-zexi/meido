import { useState, type ReactNode } from "react";
import { Dialog } from "../../ui/Dialog";
import { Icon, type IconName } from "../../ui/Icon";
import { ModelSettings } from "../model/ModelSettings";
import { OwnerKnowledgeSettings } from "./OwnerKnowledgeSettings";

/** Settings sections. Add an entry here to add a new section to the dialog. */
type SettingsSectionId = "model" | "owner-knowledge";
const sections: { id: SettingsSectionId; label: string; icon: IconName; render: () => ReactNode }[] = [
  { id: "model", label: "模型", icon: "model", render: () => <ModelSettings /> },
  { id: "owner-knowledge", label: "主人资料", icon: "memory", render: () => <OwnerKnowledgeSettings /> },
];

export function SettingsDialog({ onClose, initialSection }: { onClose: () => void; initialSection?: SettingsSectionId }) {
  const [current, setCurrent] = useState(initialSection ?? sections[0].id);
  const section = sections.find((item) => item.id === current) ?? sections[0];
  return <Dialog title="设置" onClose={onClose}>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">
        {sections.map((item) => <button key={item.id} type="button" className={item.id === current ? "active" : ""} aria-current={item.id === current || undefined} onClick={() => setCurrent(item.id)}>
          <Icon name={item.icon} size={18} />{item.label}
        </button>)}
      </nav>
      <div className="settings-content">{section.render()}</div>
    </div>
  </Dialog>;
}
