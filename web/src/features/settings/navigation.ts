export const pluginServices = [
  { id: 'channels', label: '多端连接', description: '管理聊天平台和角色账号连接。', group: '连接' },
  { id: 'mail', label: '邮件', description: '连接邮箱、管理草稿和邮件提醒。', group: '连接' },
  { id: 'mcp', label: 'MCP 服务', description: '配置本地工具服务及角色授权。', group: '连接' },
  { id: 'owner-knowledge', label: '主人资料', description: '连接资料来源，管理同步和理解。', group: '功能' },
  { id: 'todo', label: '待办', description: '管理共享待办、提醒和提醒角色。', group: '功能' },
  { id: 'novelai', label: 'NovelAI 生图', description: '设置独立凭据和账户生成额度。', group: '功能' },
  { id: 'coding', label: '编程委托', description: '登记可信仓库和专用 CLI。', group: '功能' },
] as const;
export type SettingsServiceId = typeof pluginServices[number]['id'];
export type SettingsSectionId = 'model' | 'memory' | 'appearance' | 'advanced' | 'identity' | 'plugins' | 'about' | SettingsServiceId;
export const isPluginService = (id: SettingsSectionId): id is SettingsServiceId => pluginServices.some(service => service.id === id);
