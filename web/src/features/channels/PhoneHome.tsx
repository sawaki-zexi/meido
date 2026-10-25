import { Icon } from "../../ui/Icon";
import { ChannelLogo } from "./ChannelLogo";

export type PhoneApp = {
  id: string;
  channel: string;
  label: string;
  accountName?: string;
  offline: boolean;
  history: boolean;
};

export function PhoneHome({ apps, onOpen, onSettings }: {
  apps: PhoneApp[];
  onOpen: (id: string) => void;
  onSettings?: () => void;
}) {
  return <div className={`channel-phone-home${apps.length ? "" : " is-empty"}`}>
    <ul className="channel-phone-apps" aria-label="手机应用">
      {apps.map((app) => <li key={app.id}>
        <button type="button" className="channel-phone-app" aria-label={`${app.label}${app.history && app.accountName ? ` · ${app.accountName}` : ""}${app.offline ? "，离线" : ""}`} title={app.accountName || app.label} onClick={() => onOpen(app.id)}>
          <span className={`channel-phone-app-icon role-account-mark channel-brand-mark ${app.channel}${app.offline ? " is-offline" : ""}`} aria-hidden="true"><ChannelLogo channel={app.channel} /></span>
          <span>{app.label}</span>
          {app.history ? <small>账号已移除</small> : app.offline && <small>离线</small>}
        </button>
      </li>)}
      {onSettings && <li><button type="button" className="channel-phone-settings-button channel-phone-app" aria-label="设置渠道" title="设置渠道" onClick={onSettings}>
        <span className="channel-phone-settings-icon"><Icon name="settings" size={28} /></span>
        <span>设置</span>
      </button></li>}
    </ul>
  </div>;
}
