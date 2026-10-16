export type TopicId = "roles" | "memory" | "tools" | "todos" | "mail" | "ownerKnowledge";

export type Topic = {
  id: TopicId;
  label: string;
  lines: string[];
};

export const openingLines = [
  "欢迎光临，客人。我是 Meido 的看板娘 Hiyori。",
  "Meido 是能陪伴您、帮忙处理事务的 AI 女仆……比如我。",
  "主人教了我许多本领，您想先了解哪一部分呢？",
];

export const topics: Topic[] = [
  {
    id: "roles",
    label: "角色",
    lines: ["您可以创建许多角色，每位都有自己的人设、性格和经历。"],
  },
  {
    id: "memory",
    label: "记忆",
    lines: ["角色会记住与您相处的经历……客人可别想赖账。"],
  },
  {
    id: "tools",
    label: "工具",
    lines: [
      "角色还可以搜索资料、处理文件和操作网页。",
      "遇到需要动手的事情，也可以帮忙哦。",
    ],
  },
  {
    id: "todos",
    label: "待办",
    lines: [
      "有什么要做的事，可以告诉角色，让她帮忙记下来。",
      "到了约定的时间，也会去提醒您。",
    ],
  },
  {
    id: "mail",
    label: "邮件",
    lines: ["角色可以查看新邮件，整理需要回复的内容，会以主人的口吻编写草稿。"],
  },
  {
    id: "ownerKnowledge",
    label: "主人资料",
    lines: [
      "可以导入日记、笔记和个人资料，帮助角色更好地认识自己的主人。",
      "每位角色都会结合自己的性格，慢慢形成对主人的理解。",
    ],
  },
];

export const endingLines = [
  "谢谢客人愿意听我介绍。点击链接下载项目，创建属于自己的角色吧。",
];
