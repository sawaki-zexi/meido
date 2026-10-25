import { Icon } from "../../ui/Icon";

const logos: Record<string, string> = {
  qq: "/assets/channels/qq.png",
  qqbot: "/assets/channels/qq.png",
  telegram: "/assets/channels/telegram.svg",
  feishu: "/assets/channels/feishu.ico",
};

export function ChannelLogo({ channel }: { channel: string }) {
  return logos[channel]
    ? <img className="channel-brand-logo" src={logos[channel]} alt="" />
    : <Icon name="chat" size={26} />;
}
