# Shiori-Agent 角色（Character/Role）实现研究

状态：当前
事实来源：Shiori-Agent 源码链接，以及 Meido 的 `backend/app/role_card_import.py`、`web/src/features/role-management/RoleEditor.tsx` 和 `tests/python/test_role_card_import.py`
最后核验：2026-10-23

研究对象：[YinFengWindy/Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent)，以提交 1c241a2627bbcc99124aef71abaee237ac057499 的本地源码为一手资料。本文只记录可借鉴的设计，不建议直接复制其完整架构。

本轮重新核对了 profile、manifest migration 和 Meido 当前分支实现。早期关于 Meido 迁移到 SQLite 的建议已撤回：Meido 继续使用自身 JSON manifest，新增 profile schema version 和原子迁移；当前提交是 #204 的实现分支，尚未合并。

## 1. 角色领域模型

Shiori-Agent 使用 `RoleRecord` 作为角色聚合快照，源码位于 [`apps/backend/core/roles/models.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/core/roles/models.py)。基础身份字段包括 `id`、`name`、`description`；运行时与资源字段还包括 `system_prompt`、`background`、头像、插画、`runtime_config`、渠道绑定、主动推送配置、记忆初始化状态，以及 `created_at`/`updated_at`。

角色设定被单独版本化为 `RoleProfile`（[`apps/backend/core/roles/profile_models.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/core/roles/profile_models.py)）：

```text
RoleProfile
  version: int = 1
  character: RoleCharacterDefinition
    profile: str
    personality: str
    behavior_rules: str
    response_constraints: str
    nickname: str
  import_provenance: optional metadata
```

`RoleCharacterDefinition` 是稳定的角色身份/行为输入，字段名称直接对应角色卡编辑器。`RoleProfile.from_legacy()` 可把旧的 `background` 和 `system_prompt` 映射到新结构，说明 profile 是向后兼容层，而不是替换全部旧字段的破坏性迁移。

**可借鉴点**：把“角色身份”和运行时/资源配置分开；角色设定字段使用稳定、可扩展的嵌套对象；保留 profile `version` 便于以后迁移。

**不应照搬**：Shiori 的 `RoleRecord` 还承担渠道、素材、主动推送、模型和记忆等职责，规模明显超出本 Issue。当前实现应只引入 Issue 要求的角色字段，避免把插件和运行时配置一起带入。

## 2. 持久化格式与 ID

Shiori-Agent 默认将角色保存为工作区下的 JSON 清单：[`apps/backend/core/roles/manifest.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/core/roles/manifest.py)。布局为：

```text
<workspace>/roles/roles.json
<workspace>/roles/assets/<role_id>/...
```

清单根节点包含 `version`、`roles`，并可带插件命名空间 `plugin_data`。`RoleManifestRepository` 在同一进程内对清单加锁，读取时执行迁移；迁移后用 `atomic_save_json` 一次性替换文件。`CURRENT_MANIFEST_VERSION` 当前为 9，迁移逻辑在 [`apps/backend/core/roles/migration.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/core/roles/migration.py)。

角色创建在 [`apps/backend/core/roles/store.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/core/roles/store.py) 的 `RoleStore.create_role()`：名称和 system prompt 使用 `strip()` 校验；默认 ID 为 `role-` 加短 UUID；名称不作为唯一键，因此可以存在同名角色。更新通过 `update_role(role_id, ...)` 按 ID 定位，更新时间由后端刷新。

**可借鉴点**：后端生成不可变 ID；创建/更新使用 trim 校验；保存时采用原子写入和版本迁移；角色素材放在按 ID 分隔的目录中。

**针对 Meido 的建议**：沿用现有 JSON manifest；使用角色稳定身份、独立 profile 版本和同目录原子替换。角色清单与会话、记忆 SQLite 数据库分别管理。

## 3. API/桥接流程

桌面桥接路由位于 [`apps/backend/desktop_bridge/role_requests.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/desktop_bridge/role_requests.py)：

- `roles.list` 返回序列化后的角色列表。
- `roles.create` 接收 `name`、`description`、`system_prompt`、`background`、可选 `profile`、`runtime_config` 和资源路径，创建后返回 `{ role }`。
- `roles.update` 以 `role_id` 定位，支持字段更新；profile 使用 `_merge_profile_payload()` 合并嵌套对象，避免只编辑一个子字段时丢掉其他字段。
- 参考项目还提供 `roles.delete`、角色卡导入预览/提交等能力。Meido 当前复刻其中的角色卡预览导入：只生成可审阅草稿，不直接创建角色；保存仍由现有角色编辑流程完成。

**可借鉴点**：写入响应直接返回最新角色快照，前端可用它刷新当前详情；更新 profile 时做嵌套 patch merge；所有操作以稳定 ID 传递。

## 4. 列表、创建和详情/编辑页面

界面代码在 `apps/desktop/renderer/src/roles/`：

- 列表页：[`RoleManagementPage.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleManagementPage.tsx) 渲染角色卡片；[`RoleCard.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleCard.tsx) 展示名称、简介、头像/封面和打开/对话等动作。
- 创建页：[`RoleCreatePage.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleCreatePage.tsx) 组合 [`RoleCreateFields.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleCreateFields.tsx)。基础表单先填名称、简介、头像；角色设定使用 [`RoleCardProfileForm.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleCardProfileForm.tsx)。创建时可折叠“更多设定”，默认突出 profile。
- 详情页：[`RoleDetailPage.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleDetailPage.tsx) 采用 header + toolbar + tab 内容；profile 页由 [`RoleProfilePanel.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleProfilePanel.tsx) 渲染，同一份草稿在编辑和保存之间共享；保存由 `useRoleManagement.ts` 调用 `roles.update`，成功后重新拉取角色列表并打开更新后的角色。
- 导航：[`RoleWorkspaceSidebar.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleWorkspaceSidebar.tsx) 将 `roles-list`、`role-create`、`role-detail`、`role-assets` 作为独立工作区入口。

结构化表单的具体分组是：角色设定（profile），以及性格、执行规则、回复约束；昵称属于同一 profile 数据但当前编辑器没有单独显眼的输入项。`RoleCardProfileForm.tsx` 对长文本使用可增长 textarea；创建页默认折叠非核心字段，详情页完整展开。

**可借鉴点**：列表、创建、详情是清晰的三段流程；创建成功后直接进入角色详情；详情保存后刷新服务端快照；将核心 profile 与高级字段分组，减少首次创建负担。

**不应照搬**：参考项目页面还包含账号、记忆、能力、渠道、素材等 tab；本 Issue 应保持最小角色维护页面，不提前加入这些功能。

## 5. 角色专属会话绑定

会话服务在 [`apps/backend/session/manager/role_sessions.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/session/manager/role_sessions.py) 为每个角色生成稳定 key：`role:<role_id>`，并把 `role_id` 写入 session metadata。桌面对话线程还使用 [`apps/backend/conversation/service.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/backend/conversation/service.py) 中的 `thread:<role_id>:desktop` 形式。

这说明“角色 ID 是会话绑定锚点”是可行的，但角色数据和会话数据仍由不同服务/表管理。对于 meido，不应把角色设定字段直接塞进会话记录；应先保存角色，再以 `role_id` 作为未来唯一会话的外键或派生 key。

## 6. 测试参考

参考项目把角色流程拆成可单测的纯状态/表单函数，并覆盖端到端创建：

- [`apps/desktop/renderer/src/roles/roleCreation.test.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/roleCreation.test.ts) 验证创建请求 payload、资源错误和缺失角色信息。
- [`apps/desktop/renderer/src/roles/RoleCardProfileForm.test.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleCardProfileForm.test.tsx) 验证结构化字段更新和可折叠详情。
- [`apps/desktop/renderer/src/roles/RoleDetailPage.test.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/renderer/src/roles/RoleDetailPage.test.tsx) 验证详情页关键区域。
- [`apps/desktop/tests/onboarding/electron.e2e.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/main/apps/desktop/tests/onboarding/electron.e2e.ts) 覆盖模型配置、角色创建、进入工作区和重启恢复。

对 meido 的验收建议是保留这些高价值断言：请求字段映射、空白校验、创建后列表/详情刷新、更新后重新加载、同名角色由不同 ID 区分、进程重启后仍可读。

## 7. 结论

最适合 meido 的参考组合是：

1. 使用独立 `Role`/`CharacterDefinition` 类型，profile 内按字段组织角色设定。
2. 后端生成稳定 ID，名称允许重复，更新时间由后端维护。
3. 角色列表、创建页、详情编辑页分离；创建成功后进入详情，保存后以服务端快照刷新。
4. 沿用 Meido 当前 JSON manifest 和角色目录边界，只借用显式 profile 版本与可恢复迁移；不要为了模仿 Shiori 而改成其多渠道 RoleRecord、插件命名空间或素材布局。
5. 以 `role_id` 作为未来会话绑定点，但本 Issue 只实现角色 CRUD 和维护流程。

## 8. 2026-10-23 角色 profile 核对

| 关注点 | Shiori-Agent（核验提交） | Meido #204 实现分支 | 取舍 |
| --- | --- | --- | --- |
| Profile | version、嵌套 character、可选 import_provenance；五个设定字段位于 character | version 1、嵌套 character、可选 importProvenance；API 继续使用项目 camelCase | 复用稳定嵌套边界，不复制字段命名风格 |
| Manifest | 根版本 v9、有序迁移链、JSON 原子写入和保留插件 namespace | 根版本从 v1 升至 v2；在同目录临时文件原子替换；保留根、角色、profile、能力对象中的不透明字段 | 不复制 Shiori 的 v9 schema 或 plugin_data |
| 旧数据 | 在 Role model projection 前做 migration，再构造 profile | 先变换原始 JSON，再校验整份 Role 快照；v1 的身份缺省只生成一次并随升级保存 | 防止 Pydantic projection 静默丢失未知字段，也避免启动时身份漂移 |
| 导入来源 | Shiori 记录卡片声明的 creator、tags、source、card timestamps 与实际 imported_at | Meido 只记录实际输入文件 basename、识别格式和预览时间；不把卡片声明内容混同于来源路径 | 本 Ticket 选择最小、非敏感 provenance，不凭卡片自报内容冒充文件来源 |
| 运行提示词 | provenance 在角色 profile 对象中，但角色编译器只读 character | Meido runtime / legacy adapter 只读 character；来源不会被格式化进系统提示词 | 保持相同的身份/来源边界；完整独立编译规则在 #207 |
| 角色状态 | RoleRecord 包含主动推送、transport 候选等 Shiori runtime 域 | Meido 仍保持自己的角色、唯一会话、记忆、工具策略和模型连接边界 | 只借鉴 profile/migration 设计，不合并角色与运行时职责 |

事实来源：Shiori apps/backend/core/roles/profile_models.py、migration.py、manifest.py、role_prompt_compiler.py；Meido [profile contract](../reference/role-profiles.md)、[model](../../backend/app/models.py)、[migration tests](../../tests/python/test_role_manifest_migration.py)。Shiori 的格式定义和 Meido 的实际实现不同，不能把兼容导入理解成运行时或权限兼容。

