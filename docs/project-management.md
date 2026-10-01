# 项目管理流程

本文是本项目使用 GitHub 和 AI 协作开发的简明手册。

## 一、核心原则

- GitHub Issue 记录要做什么，以及完成标准。
- GitHub Project 记录任务当前进行到哪一步。
- 默认分支 `main` 只接受经过 Pull Request 审查的代码。
- AI 负责分析、实现和测试；人负责目标、优先级、范围和最终验收。
- AI 可以起草内容，但重要决定必须由人确认。

## 二、常用名词

| 名词 | 意义 |
| --- | --- |
| Issue | 一项需求、Bug、研究、规格或开发任务 |
| 产品愿景 | 说明为什么做项目和最终想达到什么，例如 Issue #1 |
| Spec Issue | 详细功能规格，包含用户故事、范围和验收标准；通常是开发 Issue 的父 Issue |
| 开发 Issue | 从 Spec 拆出的一个可独立实现和验证的垂直功能 |
| Project | 多个 Issue 的看板和进度管理 |
| Label | Issue 的类型、状态或优先级标签 |
| Branch | 与主分支隔离的代码开发线 |
| Pull Request（PR） | 请求把分支代码合并到主分支，并接受审查 |
| CI | 自动执行测试、类型检查和构建 |
| CD | 自动发布或部署 |
| ADR | 记录重要架构决定及其原因 |

## 三、Issue 类型和标签

一个已分诊的 Issue 通常有一个类型标签、一个分诊标签和可选的优先级标签。分诊标签表示它是否可以进入下一步；Project 的状态字段记录实际开发进度。

### 类型标签

- `enhancement`：新功能或改进
- `bug`：现有功能出错
- `question`：需要澄清或讨论
- `documentation`：文档工作
- `research`：技术调研或方案验证
- `spec`：产品或功能规格
- `epic`：较大的目标或产品愿景

### 状态标签

- `needs-triage`：刚提出，等待评估
- `needs-info`：信息不足，等待补充
- `ready-for-agent`：需求完整，可以交给 AI 开发
- `ready-for-human`：代码和 CI 已准备好，需要人工审查、验收或合并
- `wontfix`：决定不处理，通常随后关闭

### 优先级标签

- `priority:p0`：紧急故障或阻塞项目，立即处理
- `priority:p1`：第一版必须完成，或影响主要流程
- `priority:p2`：重要但可以延后
- 没有优先级：还没有决定

优先级不是状态。例如 `priority:p1` 说明重要程度，`ready-for-agent` 说明是否可以开始开发。

## 四、完整工作流

```text
产品愿景
  -> 本地讨论方案，必要时创建研究 Issue
  -> Spec Issue 或架构决定
  -> 拆分并关联开发 Issue
  -> 分诊并标记 ready-for-agent
  -> AI 从最新 main 创建分支并开发
  -> Pull Request
  -> CI 检查和人工审查
  -> 合并主分支
  -> 人工验收、更新 Project 并关闭 Issue
```

### 1. 记录产品愿景

产品愿景可以创建为一个长期打开的 Issue，说明目标和可能的功能。它通常不直接交给 AI 实现，而是作为后续 Spec Issue 和开发 Issue 的来源。

### 2. 讨论技术方案

简单功能可以在本地 AI 会话中讨论；复杂或需要长期追踪的调研可以创建研究 Issue。

无论在哪里讨论，最终决定都要记录到 Issue、`CONTEXT.md` 或 `docs/adr/`，不能只留在聊天记录里。

### 3. 形成 Spec 并拆分任务

AI 可以根据已确定的方案起草 Spec Issue 和开发 Issue，但发布前由人确认：

- 目标是否准确
- 范围是否过大
- 验收标准是否可验证
- 依赖关系是否正确
- 是否明确不做什么

Spec Issue 说明一个较大功能的目标、范围和验收方向；开发 Issue 是它的子任务。每个开发 Issue 都应在正文中记录所属 Spec，例如 `Parent: #<spec-number>`。开发 Issue 之间的先后关系使用 `Blocked by` 表示，不要把父子关系和阻塞关系混为一谈。

开发 Issue 应是一个可以独立验证的垂直功能，例如“用户可以保存并查看一条 Markdown 记忆”，而不是单独的“写数据库层”。

### 4. 分诊

新 Issue 通常先标记 `needs-triage`。信息不足时改为 `needs-info`；明确且可开发时改为 `ready-for-agent`。

只有需求完整、验收标准明确的 Issue 才交给 AI 开发。

### 5. 分支开发

开始前应在 GitHub 仓库设置中保护 `main`：禁止直接 Push，要求通过 Pull Request 合并，并要求必要的 CI 检查通过。

接收 `ready-for-agent` 的 Issue 后，Agent 必须先检查当前分支和工作区状态，并自动创建对应的功能分支。确认已经进入功能分支后，才能修改或提交代码。

只讨论 Issue、阅读代码或提交实现计划时，可以暂时停留在 `main`；一旦开始编码，就不得直接修改主分支。每个开发 Issue 建立一个分支：

```powershell
git switch main
git pull --ff-only
git switch -c feat/12-markdown-memory
```

分支名称建议包含 Issue 编号和简短描述，例如：

```text
feat/12-markdown-memory
fix/18-stream-timeout
chore/20-ci-setup
```

开始开发前应确认：

```powershell
git branch --show-current
git status --short
```

当前分支应为类似 `feat/12-markdown-memory` 的功能分支，而不是 `main`。不要等到提交时才创建分支。

### 5.1 并行开发与避免重复

不要求每个 Issue 创建 Git worktree。每个 Issue 使用独立分支；开始前先检查 GitHub 上相关的开放 Issue、PR 和已合并改动，确认没有人正在交付同一结果。

- 改动范围互不依赖时，可以并行开发和同时审核多个 PR。
- 多个 Issue 依赖同一段尚未合并的代码时，在 Issue 中标明 `Blocked by`，并在前置 PR 合并后基于最新 `main` 继续集成。
- 两个任务需要修改同一个模块但没有功能依赖时，先协调改动范围；避免双方各自重写相同逻辑。
- PR 合并后，其他待合并分支不会自动更新。合并前应同步最新 `main`，解决可能的冲突并重新运行检查。

分支之间的代码只有提交并合并，或主动将提交整合到当前分支后才可见。工作目录是否分开不会自动同步代码，也不能代替 Issue/PR 协调。

### 6. Pull Request 和审核

AI 完成代码后运行测试、提交代码、推送分支并创建 PR。PR 应包含变更摘要、测试结果和关联 Issue。默认使用 `Refs #12` 关联 Issue，避免 PR 合并时自动关闭 Issue；如果项目约定“合并即代表验收”，才使用 `Closes #12`。

审核顺序：

1. CI 是否通过测试、类型检查和构建
2. Issue 的验收标准是否逐条满足
3. 是否有无关改动
4. 是否增加了必要测试和文档
5. 是否引入明显安全或数据风险

审核通过后由人合并 PR。合并后确认验收标准和部署结果，Project 状态改为 `Done`，然后关闭 Issue。若 PR 使用了 `Closes #12`，Issue 会在合并时自动关闭，此时仍需检查 Project 状态和最终验收记录。

## 五、谁负责什么

### AI 可以做

- 阅读 Issue 和项目文档
- 分析代码并提出方案
- 起草 Spec 和开发 Issue
- 创建分支、实现代码和测试
- 提交 PR，报告测试结果
- 更新进度评论和建议标签

### 人必须确认

- 产品目标和是否真的要做
- P0/P1/P2 优先级
- 技术方案和架构决定
- Issue 的范围和验收标准
- 是否扩大任务范围
- PR 是否合格、是否合并
- 是否关闭 Issue
- 是否执行邮件、教务系统等外部操作

AI 可以执行标签和 Issue 更新，但应在明确指令范围内进行；不要让 AI 自行提高优先级、关闭 Issue 或合并 PR。

## 六、开发 Issue 模板

```md
## 背景

为什么要做？

## 目标

完成后用户能做什么？

## Parent

- 由 #<spec-number> 拆分；如果没有父 Spec，写“无”。

## 验收标准

- [ ] 条件一
- [ ] 条件二
- [ ] 有必要的测试

## 不包含

- 本 Issue 不处理什么？

## Blocked by

- None — can start immediately
- 或列出必须先完成的 Issue，例如 #<number>
```

Spec Issue 至少应包含：问题、目标用户、用户故事、范围、不包含内容、整体验收标准，以及计划拆出的开发 Issue。研究 Issue 则应包含要回答的问题、调研范围和预期产出。

## 七、常用 AI 指令

### 讨论方案

```text
请先阅读 AGENTS.md、相关 Issue 和项目文档。
不要写代码。请比较可行方案，说明复杂度、风险、扩展性和推荐理由。
最后给出第一版的实现范围和模块边界。
```

### 起草开发 Issue

```text
根据已确定的方案，起草开发 Issue。
每个任务必须是一个可独立验证的垂直功能，适合在一次开发会话内完成；“不超过两天”只作为参考。
写清楚背景、目标、Parent、验收标准、不包含内容和 Blocked by。
先只输出草稿，不要发布到 GitHub。
```

### 领取并实现 Issue

```text
请领取并实现 GitHub Issue #<number>。
先检查当前 Git 分支和工作区状态。
如果当前在 main，请从最新的 main 自动创建并切换到 feat/<issue-number>-<short-description> 分支。
确认已经在功能分支后，再检查需求是否完整；不完整就列出缺失信息，不要编码。
完整后先提交实现计划；计划确认后，实现功能和测试，并创建 Pull Request。
不得直接向 main 推送；如果没有创建 PR 的权限，只推送功能分支并报告分支地址。
不要自行提高优先级、关闭 Issue 或合并 PR。
```

### 审查 PR

```text
请检查这个 PR 是否满足关联 Issue 的所有验收标准。
报告已完成、未完成、测试结果、风险和仍需我决定的事项。
不要关闭 Issue 或合并 PR。
```

## 八、完成定义

只有以下条件都满足，Issue 才算完成：

- 验收标准全部满足
- 测试、类型检查和构建通过
- PR 已人工审查并合并
- 必要的文档或 ADR 已更新
- Project 状态为 `Done`
- 人工验收完成，Issue 已关闭

