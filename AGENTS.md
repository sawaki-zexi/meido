# Meido Agent 规范

## 开始工作

- 先阅读对应 GitHub Issue、`CONTEXT.md`、相关 ADR 和本文件。
- 开始编码前检查当前分支和工作区；不要在 `main` 上修改或提交实现代码。
- 每个 Issue 使用独立分支，从最新 `main` 创建：`feat/<issue>-<description>`、`fix/<issue>-<description>` 或 `chore/<issue>-<description>`。
- 开始实现前检查 GitHub 上相关的开放 Issue 和 PR，并查看已有 PR 的目标、改动文件和进度；如果已有工作覆盖同一需求，先复用或协调，不要重复实现。
- 创建分支前先获取最新 `main`，并以它为基点。开发期间其他分支的未合并改动不会自动出现在当前分支；需要依赖它们时，先在 Issue 中记录依赖，待前置 PR 合并后再同步 `main`。
- 可以并行处理改动边界清晰、互不依赖的 Issue；若多个 Issue 要修改同一模块或共享行为，先确认依赖和实现顺序，避免并行重复改写同一逻辑。
- PR 必须以 `main` 为目标。不要从其他功能分支继续创建分支，也不要提交堆叠功能分支的 PR。
- 前置 PR 未合入时等待；前置 PR 合入后，从最新 `main` 创建后续分支。
- 实现前先提交计划，说明改动范围、数据流、测试和风险。难以逆转或影响多个功能的架构决定必须先获人工批准。

## Issue 与 Ticket

- 开发 Ticket 使用 Matt Pocock 结构：`Parent`、`What to build`、`Acceptance criteria`、`Blocked by`。
- `What to build` 描述一个从用户角度完整、可演示的结果；不要按数据库、API、前端分层拆任务。
- 验收标准必须是独立、具体、可测试的行为；不要把文件、类或实现步骤当作验收标准。
- `Blocked by` 写真正的阻塞 Issue；没有阻塞时写 `None — can start immediately`。正文关系之外，使用 GitHub 原生依赖关系。
- 多会话或大型功能先写 Spec，再拆成垂直 Ticket。经维护者确认的 Ticket 直接标记 `ready-for-agent`，不重复走 `triage`。
- `needs-triage` 只用于尚未整理的请求、Bug 和新需求；`needs-info` 表示等待补充信息。
- 关闭 Issue 前清理 `needs-triage`、`ready-for-agent` 等当前状态标签。

## 文档与领域

- `CONTEXT.md` 只记录稳定的领域词汇和关系，不记录实现细节。
- 难以逆转、存在真实取舍且未来需要解释的架构决定记录到 `docs/adr/`。
- 调研放在 `docs/research/`，规格放在 `docs/specs/`，操作手册放在 `docs/handbook/`，Agent 规则放在 `docs/agents/`。
- 文档分类、状态和事实来源规则见 `docs/agents/documentation.md`。
- 文档与源码冲突时，以当前源码和测试为准，并修正文档；不要把草案当成现状。

## 实现、测试与提交

- 默认采用测试驱动的红-绿-重构循环，测试外部行为而非实现细节。
- 完成后运行相关测试、类型检查和构建；再分别按“仓库规范”和“需求规格”进行 Code Review。
- 冲突在功能分支解决，保留当前 Issue 的目标行为，重新运行检查后推送。
- 未经人工明确批准，不合并 PR、不关闭 Issue、不执行外部事务。

## Pull Request

- 标题使用中文 Conventional Commits 格式，例如 `feat: 编辑角色设定`；一个 PR 只处理一个 Issue。
- 正文使用中文，简要说明变更目的、主要改动、验收结果、测试结果、风险或限制，并使用 `Refs #<number>` 关联 Issue。
- CI 失败、验收未完成或仍有未解决评论时不得合并。只有明确要求合并即关闭时才使用 `Closes #<number>`。

## Ticket 模板

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
