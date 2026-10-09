import { useState, type ReactNode } from "react";
import { Dialog } from "../../ui/Dialog";
import { Icon, type IconName } from "../../ui/Icon";
import { ModelSettings } from "../model/ModelSettings";
import { TodoSettings } from "../todo/TodoSettings";

/** Settings sections. Add an entry here to add a new section to the dialog. */
const sections: { id: string; label: string; icon: IconName; render: () => ReactNode }[] = [
  { id: "model", label: "模型", icon: "model", render: () => <ModelSettings /> },
  { id: "todo", label: "待办", icon: "todo", render: () => <TodoSettings /> },
];

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState(sections[0].id);
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
