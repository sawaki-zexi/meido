# 角色插画与聊天背景

状态：当前（#261，已由 PR #262 合入）。
事实来源：`backend/app/role_store.py`、`backend/app/role_assets.py`、`RoleIllustrationLibrary.tsx`；最后核验：2026-10-28。

角色插画是原尺寸图片集合，独立于头像、角色卡片图及 `workspace/memes` 表情素材。角色工作区的「素材库」先展示插画与背景，再展示原有表情素材。支持多选批量上传、下载、分类添加/重命名/移除、移动分类、搜索、选择/清除聊天背景，以及确认后移出插画。移出后原文件保留在该角色 `illustrations-removed` 恢复区，仅支持手动恢复，没有应用内恢复入口；原生成历史不变。

分类提供「移除并保留插画」（移动到其他分类）和需二次确认的「移除分类及其中插画」两种操作。后者逐张调用现有可恢复移出接口，再移除空分类；最后一个分类移除后保留空的「未分类」。批量上传和移出按顺序执行，已确认成功的结果即时保留，任一失败或结果未确认后停止后续操作并提示刷新核对，不自动重试；离开当前角色后不继续剩余批次。

## 本地接口

- `GET /api/roles/{role}/illustrations` 返回完整 `role`，含 `illustrations`（id/url/name/mediaType/size/category）、`illustrationCategories`、`chatBackgroundId` 与 `chatBackgroundUrl`。
- `POST` 同一路径接受 PNG/JPEG/WebP 原图，查询参数为 `name`、`category`；限制 10 MiB、1600 万像素、静态图。库上限 250 张及活动图片 1 GiB。
- `POST .../illustrations/from-attachment` 只接受当前角色拥有的 image 附件 ID，可通过 `setBackground` 同时设置背景；不重新生成、不发送消息、不删除原附件。
- `PATCH .../illustrations` 接收 `categories`、`bindings`（插画 ID→分类）、`categorySend`（分类名称→严格布尔许可）、`moodBindings`（心情→当前角色插画 ID）及 `defaultMood`。原子验证归属。删除分类需要同时移动仍引用它的插画；重命名同时迁移该分类许可，删除分类会清理旧许可。
- `PUT .../chat-background` 接收 `illustrationId`，`null` 恢复默认背景；不可引用其他角色插画。
- `PUT .../avatar-from-illustration` 接收 `illustrationId`，把归属插画复制为角色头像；不删除原插画。
- `GET/DELETE .../illustrations/{id}` 读取/移出当前角色插画；删除当前背景同时清除背景选择。

## 生成写回

公共 NovelAI `autoWritebackRoleAssets` 是独立、默认关闭的普通生图设置，只保存首张图并设为聊天背景，全部生成图片仍留在历史。它不替代原有角色级 CG 卡片图开关。显式重生成不继承自动写回；后台任务等待原 `publication_ready` 后才写回。同一个生成操作使用稳定 `operation_id`，重查已入库操作不重设后来修改的背景。写前仍检查当前角色/服务授权，冻结的背景 CAS 不覆盖主人后续选择。`reconcile_generated_illustration` 只检查实际文件/清单，不重新添加、恢复或更换背景。`novelai_asset_usage.library_bytes` 仅记录自动拷贝插画的累计占用；移出后的恢复区文件仍占用空间，不释放该计数。

## 上游固定来源

按 Shiori `d3acaed186b172a78a5892e0307e9affd89759f0` 的下列行为独立实现，复用 Meido 现有清单、附件和权限边界，不移植插件架构：

- `apps/backend/core/roles/store.py` 的 `_update_asset_categories`、`_update_asset_files`、`_remove_illustrations`：插画集合、分类和背景选择。
- `apps/backend/core/roles/assets.py`：角色归属路径与分类验证。
- `plugins/novelai/backend/service.py` 的 `_persist_generation`：普通生成的首图自动加入插画并设背景；重生成不隐式写回。
- `apps/desktop/renderer/src/roles/RoleAssetsPage.tsx`、`RoleAssetCategoryGroups.tsx`、`app/useRoleManagement.ts`、`app/useRolePresentation.ts` 的角色素材管理入口与聊天背景展示。

保留上游 MIT 许可于 `third_party/shiori-agent/LICENSE`；Meido 的删除改为可恢复移出，不复制上游直接删除原文件的行为。

## 心情和分类发送许可

素材页可以把一种心情绑定到一张插画，每张图只能绑定一种心情，选择同名心情会替换旧图。主人可选择默认心情。移出图片同时清理引用；没有有效绑定时回落到「平静」。字段 `moodIllustrationBindings` 和 `defaultMood` 为配置，`currentMood`/`currentMoodUpdatedAt` 为当前桌面唯一会话的呈现状态。

普通桌面主人回合在存在有效配置时获得 `set_role_mood` 工具。工具冻结角色、回合、会话和心情枚举，执行时重查原绑定和真实主人来源；撤销/改绑、停止回合、角色删除或换回合后不可执行。聊天页根据当前心情显示绑定插画，也消费现有已完成自主消息中的 `metadata.mood`，忽略其他渠道、失败消息及更早状态。它不新增模型调用，不替换 Meido 的回复格式，也不获得桌宠动作权限或图片发送权限。

分类的 `illustrationCategorySend` 对齐上游 `allow_role_send`，默认关闭，需主人单独勾选；分类名称是 Meido 当前分类身份。插画上传、设背景、绑定心情都不会隐式开启它。角色自动发送接入使用同一许可和已有会话/渠道投递边界，不把分类许可当成渠道授权；具体投递接线与回执由 `backend/app/channels/role_reactions.py`（同一 #261 集成）及原渠道链路负责。

固定行为来源另包括 `apps/desktop/renderer/src/roles/roleMoodConfig.ts`、`roleMoodBindingSelection.ts`、`roleMoodSelectors.ts`、`RoleAssetCategoryGroups.tsx`，以及 `apps/backend/core/roles/reply_state.py` 和 `plugins/meme/backend/runtime.py`。Meido 复用现有本地工具/唯一会话状态达成心情切换，不引入上游结构化回复运行时。
