<!--
状态：当前
最后核验：2026-10-31，main@90c9270
事实来源：package.json、requirements.txt、vite.config.ts、backend/app/main.py、
backend/app/storage.py、web/src/App.tsx、web/src/features/、docs/handbook/。
结构参考：https://github.com/YinFengWindy/Shiori-Agent/blob/main/README.md
-->

<div align="center">
  <h1>Meido</h1>
  <p><strong>陪你聊天，也帮你照顾日常。</strong></p>
  <p>以角色为中心的个人 AI 助手。<br />记住相处的经历，了解你的习惯，帮你处理手头的事。</p>
  <p>
    <a href="https://sawaki-zexi.github.io/meido/"><strong>项目介绍</strong></a>
    · <a href="#快速开始">快速开始</a>
    · <a href="#使用指南">使用指南</a>
    · <a href="https://github.com/sawaki-zexi/meido/issues">反馈问题</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/platform-Windows_x64-2563eb?style=flat-square" alt="Windows x64" />
    <img src="https://img.shields.io/badge/backend-Python_%2F_FastAPI-3776ab?style=flat-square" alt="Python / FastAPI" />
    <img src="https://img.shields.io/badge/frontend-TypeScript_%2F_React-3178c6?style=flat-square" alt="TypeScript / React" />
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-16a34a?style=flat-square" alt="MIT License" /></a>
  </p>
</div>

## 简介

Meido 是运行在本机的 Personal Agent，围绕角色组织设定、对话、记忆与工具。你可以创建自己的 AI 女仆，为她设定性格和经历，聊聊今天发生的事，也可以让她整理资料、记录待办或准备邮件回复。

每个角色有自己的持续对话和长期记忆。导入日记、笔记等主人资料后，角色可以结合这些内容理解你；启用主动互动后，她也可以根据获准的信息来源主动联系你，在空闲时分次推进长期任务。

除了网页聊天，角色还可以接入 Telegram、QQ 和飞书。完成主人配对后，受授权的私聊可延续你与该角色的对话背景；群聊和访客私聊保留各自的上下文。

想先了解项目，可以访问[交互式介绍页](https://sawaki-zexi.github.io/meido/)。介绍页是静态展示，实际聊天与配置在本机应用中完成。

## 主要能力

| 能力 | 可以做什么 |
| --- | --- |
| 角色与对话 | 创建、导入和导出角色卡，编辑设定、性格与回复规则；管理头像和角色素材，进行流式聊天、搜索历史，并查看工具执行过程。 |
| 长期记忆 | 保存事实、偏好与共同经历，结合关键词和向量检索召回相关内容；查看记忆时间线与文档，追溯来源，手动记录、整理或遗忘记忆。 |
| 主人资料 | 导入日记、笔记和个人资料，供角色检索和理解；主人资料与角色设定、聊天记忆分别管理。 |
| 待办与定时任务 | 在聊天中管理待办，指定角色提醒；安排到时发送消息或执行任务，并查看执行结果。 |
| 邮件助手 | 连接 QQ 邮箱，同步邮件、整理往来并准备回复草稿；最终审核与发送由你完成。 |
| 多端聊天 | 接入 Telegram、NapCat QQ、官方 QQBot 和飞书；管理角色账号，在网页查看渠道会话，并为 QQ 群配置旁听。 |
| 主动互动与长期任务 | 为角色配置主动来源、工具和投递目标；通过 Drift 长期技能，在空闲时持续积累工作文件和任务进度。 |
| 工具与扩展 | 搜索和读取网页、处理文件，按需使用 Shell、MCP 与 Skills；将合适的工作交给子 Agent 或后台任务。 |
| 浏览器与桌面操作 | 配置原生组件并启用对应插件后，让角色观察和操作网页或 Windows 窗口；屏幕读取也可用于当前对话。 |
| 生图与故事 | 使用 NovelAI 图片工作台生成图片；带着角色进入独立故事，通过行动推进剧情、保存进度并回看画面，也可关闭生图游玩文字剧情。 |
| Windows 桌宠 | 导入桌宠素材包，让角色出现在桌面，移动或播放包内动作；同时启用一个角色的桌宠。 |

多端账号、主动互动和原生工具需要分别配置。模型是否支持图片、工具调用及长上下文，取决于所选模型和服务商。

## 快速开始

当前提供源码运行方式。以下步骤面向 **Windows x64**，准备好 Git、**Python 3.12** 和 **Node.js 22.12+**（含 npm）。浏览器操作、桌面操作和桌宠的原生运行环境以 Windows 为准。

### 1. 获取源码并安装依赖

在 PowerShell 中执行：

```powershell
git clone https://github.com/sawaki-zexi/meido.git
cd meido

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci
```

### 2. 启动应用

在项目根目录打开两个 PowerShell 终端。

终端一，启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 4288
```

终端二，启动网页：

```powershell
npm run dev:web
```

打开 **<http://127.0.0.1:5288/app>**。后端默认地址为 `http://127.0.0.1:4288`；两个终端需要保持运行。

如果本机已有实例占用端口，为新实例选择独立端口和数据目录，不要复用正在使用的数据。长期使用、切换代码版本和备份恢复，参见[固定本机数据目录启动与备份](./docs/handbook/local-runtime.md)。

### 3. 配置模型，创建角色

1. 打开「设置 → 模型」，添加模型连接，填写服务地址、模型 ID 和所需凭据。自定义服务需选择匹配的 API 协议。
2. 点击「测试连接」，成功后点击「保存」。测试不会自动保存配置。
3. 新建角色或导入角色卡，填写角色设定并选择模型连接，保存后进入聊天。
4. 需要语义记忆时，在记忆设置中配置向量模型；其他能力在对应设置与插件页面按需配置。

Meido 接入了 OpenAI Chat Completions / Responses、Anthropic、Google 等模型协议。具体协议、认证方式及兼容边界见[模型接入说明](./docs/adr/0006-provider-specific-model-adapters.md)。

### 按需准备的服务

| 服务或组件 | 用途 | 何时配置 |
| --- | --- | --- |
| 对话模型 | 角色回复、工具调用与故事推进 | 开始聊天前 |
| Embedding 模型 | 长期记忆的语义检索 | 使用向量检索时 |
| Telegram / NapCat QQ / QQBot / 飞书 | 外部渠道聊天 | 接入对应账号时 |
| QQ 邮箱授权 | 邮件同步与回复草稿 | 使用邮件助手时 |
| NovelAI | 图片工作台、故事背景与 CG | 使用图片生成时 |
| MCP 服务 | 接入额外工具和主动信息来源 | 使用相应扩展时 |
| 浏览器 / 桌面原生组件 | Browser Use / Computer Use | 使用对应操作能力时 |
| Electron 宿主与桌宠素材包 | Windows 桌宠 | 启用桌宠时 |

桌宠需额外运行 `npm run setup:pet` 和 `npm run build:pet`，然后在角色资料中导入素材并启用，详见[桌宠手册](./docs/handbook/desktop-pet.md)。浏览器和桌面组件的准备步骤见下方使用指南。

## 数据与配置

Meido 面向单一主人的本机使用。按上述步骤从项目根目录启动时，数据默认保存在：

| 目录 | 内容 |
| --- | --- |
| `.data/` | 模型与功能配置、会话、记忆、待办、邮件及任务等数据；浏览器状态默认位于其 `browser-use/` 子目录。 |
| `roles/` | 角色设定、头像、附件、记忆文档及角色工作文件。 |
| `owner-knowledge-data/` | 主人资料镜像及索引。 |

这些目录已被 Git 忽略。模型配置由本地后端管理，部分服务凭据保存在系统凭据库中。迁移到其他电脑或 Windows 用户时，可能需要重新授权相关服务。

本地保存不代表离线推理：对话和所需上下文会发给你配置的模型服务；启用生图、邮件或外部渠道后，相应内容也会传给对应服务。

升级或迁移前请[创建备份](./docs/handbook/local-runtime.md#创建备份)，同一份数据只运行一个后端。仓库中的运行配置示例需要改成自己的路径。

## 使用指南

| 你想做什么 | 文档 |
| --- | --- |
| 固定数据目录、启动与备份 | [本机运行](./docs/handbook/local-runtime.md) |
| 查看、搜索和管理记忆 | [角色记忆](./docs/handbook/memory-library.md) |
| 调整回复长度与上下文预算 | [高级设置](./docs/handbook/advanced-settings.md) |
| 连接 Telegram、QQ 或飞书 | [多渠道接入](./docs/handbook/multichannel-setup.md) |
| 设置定时消息与任务 | [角色时间任务](./docs/handbook/role-schedules.md) |
| 配置外部工具 | [MCP 服务](./docs/handbook/mcp-services.md) |
| 使用内置技能 | [内置 Skills](./docs/handbook/shiori-builtin-skills.md) |
| 准备浏览器操作环境 | [Browser Use](./docs/handbook/browser-use-runtime.md) |
| 准备 Windows 桌面操作环境 | [Computer Use](./docs/handbook/computer-use-native-acceptance.md) |
| 导入并启用桌宠 | [Windows 桌宠](./docs/handbook/desktop-pet.md) |

## 本地开发

前端使用 TypeScript、React 和 Vite，后端使用 Python、FastAPI 和 SQLite；桌宠通过 Electron 连接本地后端。

完成依赖安装后，可以激活虚拟环境并同时启动前后端开发服务：

```powershell
.\.venv\Scripts\Activate.ps1
npm run dev
```

如果 PowerShell 不允许执行激活脚本，沿用快速开始中的两个终端，直接使用虚拟环境的 Python；后端命令可加 `--reload`。

常用检查如下，按改动范围选择相关测试：

```powershell
# 后端测试
.\.venv\Scripts\python.exe -m pytest tests/python

# 前端测试
npm run test:web

# 类型检查、网页与桌宠构建
npm run build

# 独立项目介绍页构建
npm run build:landing
```

| 目录 | 用途 |
| --- | --- |
| `backend/app/` | 本地 API、模型接入、Agent 执行、记忆和各项能力。 |
| `web/src/` | 主应用界面与项目介绍页。 |
| `desktop-pet/` | Electron 桌宠宿主与界面。 |
| `tests/` | 后端测试、测试夹具与集成验证。 |
| `docs/handbook/` | 配置、开发与验收手册。 |
| `docs/adr/` | 架构决定及其取舍。 |

参与开发前请阅读 [AGENTS.md](./AGENTS.md) 和[文档规范](./docs/agents/documentation.md)。领域概念见 [CONTEXT.md](./CONTEXT.md)，新增工具或能力的入口见[能力开发指南](./docs/handbook/adding-agent-capability.md)。

## 致谢

[Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent) 为 Meido 的角色、记忆与 Agent 能力设计提供了重要参考，本 README 的组织方式也参考了其项目文档。相关第三方来源和许可保留在 [third_party/](./third_party/) 中。

## License

[MIT](./LICENSE)。第三方代码和素材的许可请同时查阅各自说明。
