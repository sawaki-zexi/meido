## 项目资料

- Issue 和 PRD 使用 GitHub Issues 管理，通过 `gh` CLI 操作，详见 `docs/agents/issue-tracker.md`。
- 使用 `needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix` 标签，详见 `docs/agents/triage-labels.md`。
- 开始领域工作前阅读 `CONTEXT.md` 和相关 `docs/adr/`，详见 `docs/agents/domain.md`。

## Issue 开发

- 开发 Issue 使用 Matt Pocock 的 Ticket 结构：`Parent`、`What to build`、`Acceptance criteria`、`Blocked by`。普通功能 Issue 不强制要求背景或“不包含”章节；只有确实需要限制范围时才添加。
- `What to build` 从用户角度描述一个完整、可演示或可验证的结果，不按数据库、API、前端等技术层拆分。
- `Acceptance criteria` 必须是独立、具体、可测试的行为结果；不要把文件、类、数据库表或实现步骤写成验收标准。控制任务范围，过大的 Ticket 应继续拆分。
- `Blocked by` 必须列出真正阻塞当前 Ticket 的 Issue；没有阻塞时写 `None — can start immediately`。
- 技术实现方式通常由 Agent 在开始开发前调查并提交计划；不要在 Ticket 中提前写死未确认的文件路径、框架或代码方案。影响多个后续任务且难以逆转的架构决定必须单独确认并记录。
- 发布 Ticket 前先展示标题、交付结果、验收标准和阻塞关系，由维护者确认粒度和依赖后再发布；发布后的 Ticket 才能标记 `ready-for-agent`。
- Ticket 应按垂直切片组织：一个完成的 Ticket 应贯穿实现所需的存储、接口、界面和测试，并能独立验证。
- 多会话或较大的功能先形成 Spec，再使用 Ticket 拆分；单个 Ticket 不应承载整份产品或技术方案。
- `$triage` 只处理未整理的外部请求、Bug 和新需求。经维护者确认的 Ticket 不重复走 triage，直接在确认后标记 `ready-for-agent`。
- GitHub 上的依赖除了写在正文，还应使用可用的原生 blocking/sub-issue 关系；正文中的 `Blocked by` 作为可读备份。
- 关闭的 Issue 不得保留表示当前待处理状态的标签（如 `needs-triage`、`ready-for-agent`）；关闭前应更新为最终状态并清理过时状态标签。
- 领域术语在 `CONTEXT.md` 中维护；满足“难以逆转、没有上下文会令人意外、存在真实取舍”三个条件的架构决定记录到 `docs/adr/`。不要把长期决定只留在聊天或未提交文件中。
- 实现流程默认采用测试驱动的红-绿-重构循环；完成实现后分别从“仓库规范”和“需求规格”两个方向进行 Code Review，再提交或更新 PR。
- 开始前检查当前分支和工作区。实现前阅读 Issue、项目指引和相关代码，并提交涵盖改动、数据、测试和风险的计划；重大或难以逆转的架构决定须先获批准。
- 每个 Issue 使用独立分支：`feat/<issue>-<description>`、`fix/<issue>-<description>` 或 `chore/<issue>-<description>`。从最新 `main` 创建，PR 也必须以 `main` 为目标；不要在 `main` 上提交实现。
- 任何实现请求都先确认对应的 GitHub Issue 和当前分支；不能因为请求没有写“接受 Issue”就跳过分支流程。分支不匹配时，在编辑文件前运行 `./scripts/start-issue.ps1 -IssueNumber <编号> -Type <feat|fix|chore> -Description <kebab-case>`。脚本会在工作区干净时从最新 `origin/main` 创建分支；若工作区不干净或分支已存在，停止并保留现场，不要自动切换、stash、重置或复用别的 Issue 分支。新对话优先在 Codex 的独立 worktree 中执行。
- 新 clone 后运行 `git config --local core.hooksPath .githooks` 启用仓库提交保护；它拒绝在 `main` 或不符合 Issue 分支命名的分支提交，但不能替代实现前的 Issue/分支核对。
- 前置 Issue 尚未合入时，先等待其合入，再从最新 `main` 开始后续 Issue，避免堆叠功能分支。
- 前置 PR 若以 squash merge 合入，后续分支中的旧提交不会成为 `main` 的祖先。不要把旧分支直接 merge 到 `main`；从最新 `main` 重建后续分支，只 cherry-pick 当前 Issue 独有的提交。推送前确认 `git log main..HEAD` 和 `git diff main...HEAD` 只包含当前 Issue。
- 冲突在功能分支上解决；保留目标行为并重跑相关检查。未经人工明确批准，不合并 PR 或关闭 Issue。

## Pull Request

- 标题使用中文 Conventional Commits 格式，如 `feat: 编辑角色设定`；每个 PR 聚焦一个 Issue，不混入无关改动。
- 正文简要列出目的、主要改动、验收项、测试结果、风险/限制，并用 `Refs #<number>` 关联 Issue。只有明确要求合并即关闭时才用 `Closes #<number>`。
- 列出实际运行的测试、类型检查和构建结果；未运行的检查说明原因。界面或行为改动提供截图或复现说明。
- 等待人工审核；CI 失败、验收未完成或仍有未解决评论时不要合并。

### Ticket 模板

```markdown
## Parent

Parent: #<number>

## What to build

从用户角度描述完成后可以使用或验证的完整行为。

## Acceptance criteria

- [ ] 具体、可验证的条件
- [ ] 具体、可验证的条件

## Blocked by

- None — can start immediately
```
