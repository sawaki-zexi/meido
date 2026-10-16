# NovelAI 生成、指定图片重生成与原来源交付契约

状态：当前本地实现，#171/#172 受控验收，#173 作者验证；独立审查与准确集成以固定提交记录为准，真实账号与生成费用未验收。最后核验：2026-10-16。事实来源：[应用](../../backend/app/novelai_generation/application.py)、[参数与请求](../../backend/app/novelai_generation/parameters.py)、[API](../../backend/app/novelai_generation/api.py)、[账本](../../backend/app/novelai_generation/ledger.py)、[SessionStore](../../backend/app/session_store.py)、[真实消息与渠道测试](../../tests/python/test_novelai_main.py)。完整目标见[NovelAI 规格](../specs/novelai-generation-spec.md)，验收命令与范围见[手册](../handbook/novelai-generation-acceptance.md)。

此实现覆盖独立服务、当前主人 txt2img/img2img、指定槽重生成、受管后台、角色附件、唯一原来源交付及持久历史。#173 增加完成主人回合后的场景 CG、受限标签库与独立许可的卡片图写回；模型工具自报 `scene_cg` 仍拒绝，只有宿主观察路径有该资格。

## 配置与权限

应用服务固定 `https://image.novelai.net`；生产配置和工具不接受自定义 endpoint。独立系统 vault service 为 `meido.novelai`，键为 `token`。普通设置读取/保存、目录健康信息不读取旧 token，不访问账号诊断或试生成；只有明确设置、清空和实际单次 dispatch 使用凭据。设置/清空失败保留 cleanup pending 和不可用状态，不显示为已成功保存。

`GET/PUT /api/services/novelai` 的公开字段是 enabled=false、tokenConfigured、credentialCleanupPending、revision、allowedModels、defaultModel、allowedSamplers、defaultSampler、maxPixels、maxSteps、dailyAttemptLimit。PUT 另须 expectedRevision；变更推进版本并撤销在途资格。默认 curated v4.5、Euler ancestral、1,048,576像素、28步、账户20次/UTC日；full 模型只能由主人明确加入 allowlist。各值有有限上限；次数不是 Anlas 估价。

独立 token 操作是 `PUT /api/services/novelai/token`（严格仅 token 字段）及 DELETE 同路径。不存在 token 查看接口。请求和错误不回显秘密、原始响应正文或输入图片。

角色 `agentConfig.novelai` 默认 manualEnabled=false、manualDailyAttempts=10、sceneCgEnabled=false、sceneCgDailyAttempts=3、scenePromptModelConfigurationId=null、autoWritebackRoleAssets=false、promptTagsEnabled=false、allowedPromptTagRatings=[general]、consentRevision=0。手动和 CG 许可独立；启用任一消费能力须服务已配好且明确同意当前 revision。CG 还须明确选中存在的模型配置 ID，null/已删除 ID 不回退全局模型；写回须同时启用 CG。降低额度不清除计数。旧客户端省略整个字段或只提交部分字段时保留既有许可；新增标签默认值不改变旧 #172 请求的权限指纹。实际许可变化在角色文件写入前推进私有持久 epoch，关闭再开启也不能复活旧冻结工具。手工写 enabledTools 不能绕过许可。

工具 `generate_image` 为 direct / risk external / source builtin:novelai / external_allowed=false，外层330秒、输出4000字符。只接受宿主真实主人当前输入：桌面 SQLite run 必须由真实 `/messages` 写入 humanInputSource=desktop_owner；渠道须真实当前主人 owner context、账号/配对/目录代际及原线程。访客、定时、内部完成、主动或伪造 source 不能获得消费权。执行前、外部投递前和本地发布前均重新检查；冻结版本只能收权。

## 参数与身份

所有参数对象额外字段拒绝；角色、来源、token、endpoint、path 等不能由模型传入。参数包括英文 ASCII prompt/negative_prompt（各最多4000字符）、必填 mode、img2img 当前附件ID、尺寸、1–28整数 steps、uint32 seed、allowlist model/sampler，以及仅 img2img 的有限 strength/noise。bool/字符串不代替数字，NaN/Infinity 拒绝。省略 seed 时宿主先生成并持久再发送。

尺寸为 square=1024×1024、landscape=1216×832、portrait=832×1216，或每边64–1024且64倍数的 custom；面积受当前服务上限约束。固定 n_samples=1，v4.5 完整请求常数保留在 parameters.py。当前消息/宿主核验引用图片ID才可成为工具底图；桌面来源的显式 UI 可选择本角色当前可解析的 owned 附件。渠道重生成仅继承宿主冻结的原来源图片 allowlist，并重新校验当前来源许可与角色附件文件；桌面管理历史不能将未获原渠道授权的私有图片加入该 allowlist。输入最多10MiB并完整解码，拒绝 raw path/URL、跨角色或普通工具调用选择任意历史附件。

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

发送器已返回而本地 journal 暂时不可写时，宿主持有原 receipt 的本地结算义务；文件系统恢复后，维护循环仅补写 journal、SQLite 和原消息，不再次发送。该暂存不把实际 sent 改为 uncertain；发送器尚未返回的 sending 记录不因此提前结算。若进程在 journal 成功前退出，仍按已持久化的 sending 事实在重启后结算 uncertain，不能重发。

本地恢复器是应用拥有并 join 的维护任务，只处理已登记的媒体、原消息、receipt和 ACK，不是第二个通用任务宿主，不重新出网生成或重发渠道消息。每批最多100条，按创建时间和ID稳定排序，以持久游标轮转；暂时无法查询 Runtime 的旧义务保留但不会反复占满同一批，游标到末尾后回到起点。无待同步消息的 suppressed 墓碑不再作为消息同步义务；并发恢复调用仅由一轮执行。

## 应用接口与历史

### 主人回合场景 CG

[场景协调器](../../backend/app/novelai_generation/scene.py)由原宿主持有。真实主人输入被接受时，同步推进本来源 human/scene revision 并封存旧 CG，再登记 owned 取消收尾；主聊天无需等待旧网络退出，下一 CG 则等待该收尾。原主动回合优先级和完成回调继续组合执行。当前主人回复真正完成后最多一次候选；访客、旁听、内部完成、主动回合及失败回复没有资格。该回合已有手动意图（包括 unknown）时不观察，持久 human-run claim 和账户唯一 slot 阻止并发消费。

完成后的可选候选登记遇到本地I/O或SQLite写锁时，保留已完成聊天及原渠道正常发布，不触发辅助/生成或自动重放。该错误边界不吞接受主人时的同步seal或已拥有任务的取消收尾责任。

辅助调用与生图属于同一 attempt、同一个 300 秒 TaskRuntime deadline。明确 ID 的 rich adapter 只请求一次，tools=()、max_tokens=600，辅助阶段至多30秒；没有单独模型守护进程、重试或第二次生成 POST。输入仅当前 user、完成 assistant、角色描述及本来源最小前态，不含其他会话。输出严格 JSON、总16KiB，拒绝重复/额外字段、工具调用、无效枚举或越界文本。返回时以 humanRun、scene revision、模型 epoch、角色/来源许可和标签版本再次校验；配置修改/删除、关闭再开及迟到结果不能获得新资格。

none/closed、冷却和重复视觉仍是已发生辅助调用的 CG 尝试，账户与 CG 日额度不退；只有辅助和生成均未出网才释放次数。same 必须匹配当前场景，需距上次生成 dispatch 至少5个真实主人回合；started/changed可越过冷却，但不能越过已 dispatch 的 visual_key 墓碑、预算或许可。visual 墓碑最多每角色10,000条，满时拒绝新的生成；不删除旧墓碑以放行重复。重启不继续未确认候选，也不发布旧迟到 CG；已有 sent receipt 仍仅修复原消息事实。

历史新增 purpose=manual/scene_cg、phase=auxiliary/generation/completed、auxiliaryConsumption 与 auxiliaryStatus；辅助阶段没有冻结生图参数时 parameters=null。completed_without_image 表示本次观察完成但未生图，其原因包括 scene_none、scene_closed、scene_cooldown、scene_visual_duplicate；失败辅助为 scene_observation_failed。生成和辅助消费可能性分开显示，不把“没图”说成免费或退款。

### 标签和卡片图

[标签库](../../backend/app/novelai_generation/prompt_tags.py)仅由主人管理当前角色，最多250项；name最多80字符、keywords为1–16个各最多80字符的非空字面词、正负提示词各最多4000个 ASCII 字符，rating限定general/mature/explicit。匹配对当前消息做 NFKC/casefold 字面包含，不执行正则或表达式；同时过滤角色 promptTagsEnabled、allowedPromptTagRatings 和条目 enabled。预览与实际 CG 使用同一扩写，最终提示词仍受4000字符上限；库修改推进 revision，旧观察不能使用新标签结果继续生成。

- GET/POST `/api/roles/{roleId}/novelai/prompt-tags`：最多50项分页及 owner 新增，返回 tags/revision/nextCursor 或 tag/revision。
- PUT/DELETE `.../{tagId}`：仅当前角色既有32位十六进制 ID，更新或删除并推进 revision，跨角色404。
- POST `.../preview`：text最多12000字符，prompt/negative_prompt各最多4000 ASCII，返回扩写、matchedIds、revision；不调用模型或生图。

[资产写回](../../backend/app/novelai_generation/assets.py)仅在 autoWritebackRoleAssets 明确开启、当前 CG 获准且单图时，设置本角色 cardImage；不改头像。以候选完成时捕获的 cardImageUrl 做 RoleStore 版本 CAS，主人后来手选卡片图优先。生成附件之外的卡片实际字节副本，在写入前另预留角色和全局空间，包含替换时旧副本与新副本的峰值。正常历史及原来源交付不因写回失败重生成。

writebackStatus为not_requested/writing/uncertain/applied/failed。manifest或SQLite故障保留本地义务，维护只核对 owned 临时/备份、manifest及摘要后结算或回滚；崩溃在文件替换后但manifest提交前时恢复旧卡片，不再生成、复制或覆盖后来的主人编辑。恢复每批最多100条并有持久轮转游标。无法确认旧文件事实时保留预占及 uncertain。

`background=true` 先写入有限 attempt、原来源和唯一 outbox，再以同一 submission_key 交接现有 TaskRuntime。只有 taskId 及 `background_accepted` 持久成功后才返回 accepted（工具）或 HTTP202（UI），公开 `backgroundAccepted=true`。父回合完成、父 token 取消或受理后的 HTTP 断开不会撤回这次明确后台；显式停止、许可/来源失效、角色删除和应用退出仍 seal、stop 并 join。交接回写失败不假报受理，不留下隐式付费后台。

交付模式为 foreground_reply、source_completion、replacement 或主人明确选择的 history_only，outbox 主键始终只有 attemptId。后台完成只在原角色锁可用后发布，忙角色以有界试锁跳过并保留恢复义务。桌面和渠道都在现有原 session 写唯一 internal_completion，无 user/run；渠道先持久 sending/consumed，再由真正 ChannelHost.account_send(actor=reply) 向冻结的原 account/thread/chat 发送 owned 附件。原入站的回复/群提及/话题信息来自宿主快照，模型和 UI 没有改投字段。媒体能力不支持时，在生成 POST 前拒绝自动交付；仅历史必须由主人另行明确选择。

重生成是新的明确主人动作：当前配置和 grant 重新核验，新 attempt 关联同一 generation 与 parentAttemptId，省略 seed 时宿主生成与原图不同的 seed，显式沿用旧 seed 拒绝。渠道图片的确认界面在再次消费前说明会向原渠道再次发送一条新的媒体消息；明确选择仅保存历史时，不向原渠道或会话发送媒体。同 requestId 的参数/目标/模式指纹一致才重查原 attempt，不增加 POST。SessionStore.replace_attachment_once 在调用者的同库事务中只替换 expectedAttachmentId，保留其他图、文件、metadata 和顺序；canonical receipt 对键顺序无关，同 attempt 重放不改写。新动作若已能确认原槽不存在，在消费前409拒绝并提示选择仅历史；同 requestId 重查仍返回原事实。生成期间旧槽或消息改变则新图保留历史，deliveryStatus=history_only、errorCode=replacement_conflict，不再次消费。意外多图仍保留 unexpected_multiple_images，不覆盖为槽位冲突。

普通前台已收图而原最终回复失败/不可用时，原角色锁释放后仅能在未消费的同一 outbox 内 CAS 到原来源完成模式；新模式不能绕过 seal 或当前授权。已 sending/sent/uncertain/consumed 的记录不切换模式或重发。渠道实际 sent receipt 的 journal 可在重启后修复 SQLite/消息，已知 sent 不降级；没有 receipt 的 sending 重启为 uncertain，原消息也投影为 uncertain，绝不补发。消息投影与 message_synced 标志同事务，渠道更新事件只公开安全消息投影。

- POST `/api/roles/{roleId}/generations`：可信 UI 操作，requestId、expectedRevision、parameters，另可明确 deliveryMode=history_only。参数 background 默认 false；前台等待结果，后台完成交接后 HTTP202 返回同一 generation 投影。
- GET 同路径：默认20/最多50、nextCursor 分页及持久尝试/存储使用量；桌面主人可明确管理该角色所有来源。
- GET `.../{generationId}?attemptId=...`：该角色、该谱系的精确 attempt；省略 attemptId 兼容返回最新记录。
- POST `.../{generationId}/cancel`：body={attemptId} 停止精确本地尝试并封存展示，不承诺远端退款；省略 body 兼容旧客户端停止最新记录。
- POST `.../{generationId}/regenerate`：requestId、expectedRevision、sourceAttemptId、expectedAttachmentId、parameters，另可 deliveryMode=replacement（默认）或明确 history_only。
- GET `.../attachment/{attachmentId}?messageId=...`：核对当前消息、角色/session、canonical outbox、实际输出及 generationAttemptIds 后返回精确生成记录；不匹配404，不扫描所有历史。

工具查询严格留在实际来源；桌面管理接口的跨来源历史权不能被渠道模型借用。状态包括 prepared、dispatching、received、succeeded、local_pending、unknown、failed_response、cancelled_before_dispatch、rejected；generation 与 delivery 状态分开。图片归属文件和历史不随 TaskRuntime日志 prune 消失。

V4.5 请求常数改编自 Shiori-Agent 固定 `4f266aeaf42fcd611362e2456417b91f659341cf`（Copyright 2026 YinFengWindy，MIT）；完整许可随[模块](../../backend/app/novelai_generation/SHIORI-LICENSE.txt)保留。源码许可不替代 NovelAI 服务条款或图片权利。
