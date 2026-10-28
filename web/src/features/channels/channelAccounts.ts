export type Channel = "telegram" | "qq" | "qqbot" | "feishu" | "grok_bot";

export type Capabilities = {
  private?: boolean;
  group?: boolean;
  groupListening?: boolean;
  media?: boolean;
  replies?: boolean;
  streaming?: boolean;
  targets?: string[];
};

export type Account = {
  accountId: string;
  channel: Channel;
  enabled: boolean;
  platformEnabled?: boolean;
  effectiveEnabled?: boolean;
  allowPrivate: boolean;
  allowGroup: boolean;
  blockedSenders: string[];
  displayName: string;
  platformId: string | null;
  endpoint?: string;
  appId?: string;
  lastError: string | null;
  tokenConfigured: boolean;
  credentialsConfigured?: boolean;
  capabilities?: Capabilities;
  connectionStatus?: "connected" | "disconnected" | "connecting" | "error" | "unknown";
  avatarUrl?: string | null;
  mode?: "managed" | "external";
  managedRef?: string;
  maxMessages?: number;
  routine?: string;
};

export type ChannelSettings = {
  channel: Channel;
  name: string;
  enabled: boolean;
  configuredAccounts: number;
  connectedAccounts: number;
};

export type ManagedQQSession = {
  ref: string;
  available: boolean;
  preparation: { stage: string; percent: number; version?: string | null; error?: string | null };
  login: { phase: string; loginPhase?: string; qrcode?: string | null; uin?: string | null; nickname?: string | null; error?: string | null };
};

export const channels: { id: Channel; name: string; summary: string }[] = [
  { id: "telegram", name: "Telegram", summary: "Bot 私聊与群聊" },
  { id: "qq", name: "QQ", summary: "扫码连接 · 私聊、群聊与群旁听" },
  { id: "qqbot", name: "官方 QQBot", summary: "C2C 私聊" },
  { id: "feishu", name: "飞书", summary: "Bot 私聊" },
  { id: "grok_bot", name: "Grok Bot", summary: "固定 Bot 文字私聊 · 同机客户端回程" },
];

export const channelName: Record<string, string> = {
  desktop: "桌面", telegram: "Telegram", qq: "QQ", qqbot: "官方 QQBot", feishu: "飞书", grok_bot: "Grok Bot",
};

export const defaultCapabilities: Record<Channel, Capabilities> = {
  telegram: { private: true, group: true, groupListening: false, media: true, replies: true, streaming: false, targets: ["private", "group"] },
  qq: { private: true, group: true, groupListening: true, media: true, replies: true, streaming: false, targets: ["private", "group", "group_member"] },
  qqbot: { private: true, group: false, groupListening: false, media: true, replies: true, streaming: true, targets: ["private"] },
  feishu: { private: true, group: false, groupListening: false, media: true, replies: true, streaming: true, targets: ["private"] },
  grok_bot: { private: true, group: false, media: false, replies: true, streaming: false, targets: ["private"] },
};

export function accountStatus(account: Account | null, platformEnabled = true): string {
  if (!platformEnabled || account?.platformEnabled === false) return "平台已暂停";
  if (!account) return "未连接";
  if (!account.enabled) return "已暂停";
  if (account.connectionStatus === "connected") return account.channel === "grok_bot" ? "本机入口已启用" : "已连接";
  if (account.connectionStatus === "connecting") return "正在连接";
  if (account.connectionStatus === "error" || account.lastError) return "连接异常";
  if (account.connectionStatus === "disconnected") return "未连接";
  return "正在连接";
}
