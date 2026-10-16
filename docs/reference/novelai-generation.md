# NovelAI 生成、指定图片重生成与原来源交付契约

状态：当前本地实现，#171/#172 受控验收；真实账号与生成费用未验收。最后核验：2026-10-16。事实来源：[应用](../../backend/app/novelai_generation/application.py)、[参数与请求](../../backend/app/novelai_generation/parameters.py)、[API](../../backend/app/novelai_generation/api.py)、[账本](../../backend/app/novelai_generation/ledger.py)、[SessionStore](../../backend/app/session_store.py)、[真实消息与渠道测试](../../tests/python/test_novelai_main.py)。完整目标见[NovelAI 规格](../specs/novelai-generation-spec.md)，验收命令与范围见[手册](../handbook/novelai-generation-acceptance.md)。

此实现覆盖独立服务、当前主人 txt2img/img2img、指定槽重生成、受管后台、角色附件、唯一原来源交付及持久历史。场景 CG、标签和资产自动写回仍由 #173 实施；模型自报 `scene_cg` 和提前开启 CG/资产写回会明确拒绝。

## 配置与权限

应用服务固定 `https://image.novelai.net`；生产配置和工具不接受自定义 endpoint。独立系统 vault service 为 `meido.novelai`，键为 `token`。普通设置读取/保存、目录健康信息不读取旧 token，不访问账号诊断或试生成；只有明确设置、清空和实际单次 dispatch 使用凭据。设置/清空失败保留 cleanup pending 和不可用状态，不显示为已成功保存。

`GET/PUT /api/services/novelai` 的公开字段是 enabled=false、tokenConfigured、credentialCleanupPending、revision、allowedModels、defaultModel、allowedSamplers、defaultSampler、maxPixels、maxSteps、dailyAttemptLimit。PUT 另须 expectedRevision；变更推进版本并撤销在途资格。默认 curated v4.5、Euler ancestral、1,048,576像素、28步、账户20次/UTC日；full 模型只能由主人明确加入 allowlist。各值有有限上限；次数不是 Anlas 估价。

独立 token 操作是 `PUT /api/services/novelai/token`（严格仅 token 字段）及 DELETE 同路径。不存在 token 查看接口。请求和错误不回显秘密、原始响应正文或输入图片。

角色 `agentConfig.novelai` 默认 manualEnabled=false、manualDailyAttempts=10、sceneCgEnabled=false、sceneCgDailyAttempts=3、scenePromptModelConfigurationId=null、autoWritebackRoleAssets=false、consentRevision=0。启用 manual 必须服务已配好且明确同意当前 revision；降低额度不清除计数。旧客户端省略整个字段或只提交部分字段时保留既有许可。实际许可变化在角色文件写入前推进私有持久 epoch，关闭再开启也不能复活旧冻结工具。手工写 enabledTools 不能绕过许可。

工具 `generate_image` 为 direct / risk external / source builtin:novelai / external_allowed=false，外层330秒、输出4000字符。只接受宿主真实主人当前输入：桌面 SQLite run 必须由真实 `/messages` 写入 humanInputSource=desktop_owner；渠道须真实当前主人 owner context、账号/配对/目录代际及原线程。访客、定时、内部完成、主动或伪造 source 不能获得消费权。执行前、外部投递前和本地发布前均重新检查；冻结版本只能收权。

## 参数与身份

所有参数对象额外字段拒绝；角色、来源、token、endpoint、path 等不能由模型传入。参数包括英文 ASCII prompt/negative_prompt（各最多4000字符）、必填 mode、img2img 当前附件ID、尺寸、1–28整数 steps、uint32 seed、allowlist model/sampler，以及仅 img2img 的有限 strength/noise。bool/字符串不代替数字，NaN/Infinity 拒绝。省略 seed 时宿主先生成并持久再发送。

尺寸为 square=1024×1024、landscape=1216×832、portrait=832×1216，或每边64–1024且64倍数的 custom；面积受当前服务上限约束。固定 n_samples=1，v4.5 完整请求常数保留在 parameters.py。当前消息/宿主核验引用图片ID才可成为工具底图；显式 UI 可选择本角色当前可解析的 owned 附件。输入最多10MiB并完整解码，拒绝 raw path/URL、跨角色或普通工具调用选择任意历史附件。

一次实际 human run 只有一个持久消费意图 claim，换 call/intent 也不能再消费。同调用/同 UI requestId 参数一致返回原记录，冲突拒绝；当前授权仍须成立。重查先找保存的 snapshot，不重新随机 seed，不因原底图随后删除而重新消费。新的明确 UI 操作才创建新的请求键。

## 执行、恢复与资源

账本与 SessionStore 使用同一 SQLite 数据库的独立业务表。prepared 及有限额度、96MiB存储预占、账户唯一在途 slot 在同一事务；UTC 日取当前日与已用最大日，时钟回退不重置额度。每角色生成媒体1GiB、全局5GiB，出网前必须有足够预留。unknown 保留尝试占用；明确出网前拒绝/取消才释放计数。

执行由现有 TaskRuntime 拥有，kind=novelai_image、300秒、日志64KiB、尾部4000字符、completion=false、submission_key=attempt_id、retain_until_ack=true，受既有全局8/角色3并发约束。已准入但 taskId 回写失败时，前台和后台都必须 stop/join，后台不得假报 accepted；恢复按同一 Runtime handover 找回结果，复制业务事实后本地 ACK。ACK/标记失败不会重新提交或 POST。Runtime cancelled/timed_out 与业务 unknown/已收图可以不同。

每 attempt 最多一次生成 POST。HTTP200/201 接收固定 ZIP/对象JSON/原始受支持图片，显式禁重试、重定向、环境代理/证书隐式配置；HTTP期限沿同一原始 deadline。已知400/401/402/403/422可为 failed_response；408/429/5xx、断线、超时、取消及坏包为 unknown，均不承诺未扣费或退款。

响应 raw spool≤32MiB、总解压≤64MiB、最多16项、每图像素≤本次批准面积。按实际读取计数，不信任长度声明或 ZIP 路径。Pillow 完整验证与加载；每图直接写角色附件的 staging 再 rename，不另存完整解压副本。staged 附件引用先持久，再删除完整 spool，最后结算 succeeded；本地故障 local_pending 仅重做本地步骤，复用已写二进制。坏包中已验证图片保留历史，按真实 raw spool/图片/孤立二进制结算，不丢历史、不用重新生成补偿。

正常完成附件为公共 id/name/mediaType/size/kind，无路径/base64。意外多图完整保存，记录 unexpected_multiple_images；工具报协议异常，canonical outbox 仅留历史，不作为正常原回复自动展示或发送。

## 唯一展示与交付

每 attempt 只有一条 canonical media outbox。桌面真实 `/messages` 在 run/final assistant text 都完成后，通过 `append_generation_attachments_once` 在同一 SQLite 事务追加附件和消费 outbox，保留旧 metadata/其他附件。显式 UI 生成使用 `append_generation_completion_once` 向已存在的原角色桌面 session 插入唯一 internal_completion；不伪造 user/run，也不新建第二会话。

普通前台 UI HTTP 断连/取消先 fsync 原请求的私有取消墓碑并封存发布资格，再停止并等待 Runtime；即使导入稍后成功，恢复也不得迟到插入消息。图片和真实消费事实仍可在原角色历史查看。关闭、撤权、角色删除都等待已拥有任务收尾；删除只清本角色历史/墓碑/媒体，保留全局 token 和账户当日已消费计数。

渠道只在原 owner 来源和 adapter 支持图片时准入；含生成能力的回合等待最终回复，再以原账号/目标一次发送文本与附件。发送前持久 sending/consumed；真实 receipt 先写原角色受管小 journal，再复制业务表，后续 SQLite/消息写失败仅本地恢复。sent 不降级为 failed；真实丢回执为 uncertain，重启 sending 亦转 uncertain，不补发、不改投桌面或私聊。

本地恢复器是应用拥有并 join 的维护任务，只处理已登记的媒体、原消息、receipt和 ACK，不是第二个通用任务宿主，不重新出网生成或重发渠道消息。每批最多100条，按创建时间和ID稳定排序，以持久游标轮转；暂时无法查询 Runtime 的旧义务保留但不会反复占满同一批，游标到末尾后回到起点。无待同步消息的 suppressed 墓碑不再作为消息同步义务；并发恢复调用仅由一轮执行。

## 应用接口与历史

`background=true` 先写入有限 attempt、原来源和唯一 outbox，再以同一 submission_key 交接现有 TaskRuntime。只有 taskId 及 `background_accepted` 持久成功后才返回 accepted（工具）或 HTTP202（UI），公开 `backgroundAccepted=true`。父回合完成、父 token 取消或受理后的 HTTP 断开不会撤回这次明确后台；显式停止、许可/来源失效、角色删除和应用退出仍 seal、stop 并 join。交接回写失败不假报受理，不留下隐式付费后台。

交付模式为 foreground_reply、source_completion、replacement 或主人明确选择的 history_only，outbox 主键始终只有 attemptId。后台完成只在原角色锁可用后发布，忙角色以有界试锁跳过并保留恢复义务。桌面和渠道都在现有原 session 写唯一 internal_completion，无 user/run；渠道先持久 sending/consumed，再由真正 ChannelHost.account_send(actor=reply) 向冻结的原 account/thread/chat 发送 owned 附件。原入站的回复/群提及/话题信息来自宿主快照，模型和 UI 没有改投字段。媒体能力不支持时，在生成 POST 前拒绝自动交付；仅历史必须由主人另行明确选择。

重生成是新的明确主人动作：当前配置和 grant 重新核验，新 attempt 关联同一 generation 与 parentAttemptId，省略 seed 时宿主生成与原图不同的 seed，显式沿用旧 seed 拒绝。同 requestId 的参数/目标/模式指纹一致才重查原 attempt，不增加 POST。SessionStore.replace_attachment_once 在调用者的同库事务中只替换 expectedAttachmentId，保留其他图、文件、metadata 和顺序；canonical receipt 对键顺序无关，同 attempt 重放不改写。新动作若已能确认原槽不存在，在消费前409拒绝并提示选择仅历史；同 requestId 重查仍返回原事实。生成期间旧槽或消息改变则新图保留历史，deliveryStatus=history_only、errorCode=replacement_conflict，不再次消费。意外多图仍保留 unexpected_multiple_images，不覆盖为槽位冲突。

普通前台已收图而原最终回复失败/不可用时，原角色锁释放后仅能在未消费的同一 outbox 内 CAS 到原来源完成模式；新模式不能绕过 seal 或当前授权。已 sending/sent/uncertain/consumed 的记录不切换模式或重发。渠道实际 sent receipt 的 journal 可在重启后修复 SQLite/消息，已知 sent 不降级；没有 receipt 的 sending 重启为 uncertain，原消息也投影为 uncertain，绝不补发。消息投影与 message_synced 标志同事务，渠道更新事件只公开安全消息投影。

- POST `/api/roles/{roleId}/generations`：可信 UI 操作，requestId、expectedRevision、parameters，另可明确 deliveryMode=history_only。参数 background 默认 false；前台等待结果，后台完成交接后 HTTP202 返回同一 generation 投影。
- GET 同路径：默认20/最多50、nextCursor 分页及持久尝试/存储使用量；桌面主人可明确管理该角色所有来源。
- GET `.../{generationId}?attemptId=...`：该角色、该谱系的精确 attempt；省略 attemptId 兼容返回最新记录。
- POST `.../{generationId}/cancel`：body={attemptId} 停止精确本地尝试并封存展示，不承诺远端退款；省略 body 兼容旧客户端停止最新记录。
- POST `.../{generationId}/regenerate`：requestId、expectedRevision、sourceAttemptId、expectedAttachmentId、parameters，另可 deliveryMode=replacement（默认）或明确 history_only。
- GET `.../attachment/{attachmentId}?messageId=...`：核对当前消息、角色/session、canonical outbox、实际输出及 generationAttemptIds 后返回精确生成记录；不匹配404，不扫描所有历史。

工具查询严格留在实际来源；桌面管理接口的跨来源历史权不能被渠道模型借用。状态包括 prepared、dispatching、received、succeeded、local_pending、unknown、failed_response、cancelled_before_dispatch、rejected；generation 与 delivery 状态分开。图片归属文件和历史不随 TaskRuntime日志 prune 消失。

V4.5 请求常数改编自 Shiori-Agent 固定 `4f266aeaf42fcd611362e2456417b91f659341cf`（Copyright 2026 YinFengWindy，MIT）；完整许可随[模块](../../backend/app/novelai_generation/SHIORI-LICENSE.txt)保留。源码许可不替代 NovelAI 服务条款或图片权利。
