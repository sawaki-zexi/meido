# Drift 表情库维护契约

状态：当前功能分支已实现，本地组合验收与总 PR 集成以验收记录为准。事实来源：[批准规格](../specs/drift-meme-maintenance-spec.md)、[配置](../../backend/app/proactive_runtime/config.py)、[库模块](../../backend/app/proactive_runtime/meme_library.py)、[真实运行测试](../../tests/python/test_drift_meme_api.py)。最后核验：2026-10-17。

## 独立用途许可

`GET/PUT /api/roles/{roleId}/autonomy` 的 `config.driftMeme` 保存以下有限许可。PUT 仍须携带当前 `expectedRevision`。

| 字段 | 默认值与含义 |
| --- | --- |
| `enabled` | `false`；显式采用当前角色固定表情库 |
| `visionModelConfigurationId` | `null`；须显式选择与当前 Drift 相同、支持图片的已保存模型连接 |
| `readEnabled` | `false`；实际查看库内容 |
| `editEnabled` | `false`；维护获准类别的描述、别名及启用状态 |
| `copyEnabled` | `false`；窄复制冻结的本角色上传素材，与分类编辑许可分开 |
| `allowedCategories` | 空；最多 32 个唯一规范目录名，每个最多 128 字符 |
| `sourceAttachmentIds` | 空；最多 16 个唯一上传 ID，每个最多 128 字符；运行时核实真实归属与文件 |
| `managedEpoch`、`revision` | 宿主分配，客户端保留 GET 值；不接受客户端生成的新身份 |

用途启用需要实际库读许可和有限类别范围。模型、角色、配置和根身份在每次 tick 固定，操作与实际图像发送前继续核实。模型连接无聊天、主屏或其他角色回退。目录查询不会读取图片或访问模型。

这份用途可独立于 `driftAuthoring.enabled` 运行，资格只授予完整字节 hash 匹配的内建 S04。原普通文件许可、Drift 文件 grant、固定模式声明及用途许可仍同时约束操作。S06、自定义或同名角色技能继续按自己的作者与信任合同执行。

## 内建模式与文件语义

普通聊天 S04 保留 `read_file/list_dir/write_file/edit_file/shell` 声明。其固定 1.1.0 Drift profile 为 `read_file/list_dir/write_file/edit_file/meme_copy_image/finish_drift`，不需要 Shell 或白名单。完整 SKILL 与唯一 profile 支持文件共同参与发现、依赖、激活、schema 和 dispatch 的身份核验。

Drift 普通相对路径仍只访问私有 Drift 根；`@memes/` 明确映射当前角色 `workspace/memes`。分类写仅接受 manifest，保留未获准类别；新图只能用 `meme_copy_image(attachment_id, category)`，宿主选择新名称。工具返回原素材 public ID、实际副本路径/hash/字节与操作 ID，不创建新的发送身份。没有 public ID 或新 metadata 的旧库图片仍能读取、维护。

从未启用或停用后的普通主人 FileTools 保持原合同。启用时，主人与 Drift 的库写共同遵守本 run 的实际读观察与库版本 CAS：已有目标未读返回 `needs_read`，旧观察冲突须重新读；本 run 自己完成 copy 也会推进库版本，随后编辑须重新读取 manifest。其他文件不参加该新增合同。真实准备、解码、缩放、扫描、fsync 与 hash 不持发布路径锁；最终准入和 replace 使用共享短临界段。受管动作每次取路径锁最多等待 250ms，可能返回 `path_busy`；这不是 OS 操作完成时限。

停用先 seal 原代际，随后原 owner 自然取消、join。新主人普通编辑不等待慢 provider、图像或文件 worker；已在旧用途准入的动作若被 seal，须按当前许可重新发起。重新启用若旧资源尚未退出返回 409，退出后生成新 epoch。停用、同名重建与恢复都不会复活旧 run。

## 实际阶段与恢复

`GET /api/roles/{roleId}/autonomy/meme-library` 返回当前 `enabled`、最近最多 100 个安全 `operations` 和最近最多 100 个 `modelRequests`，用于有限诊断，不承诺是完整历史导出。

操作公开 `operationId/runId/callId/path/status/physicalPhase`。持久意图保留原身份、权限代际、expected 文件/root/hash/版本、全部 planned 内容及有限预留。阶段包括 `reserved/files_ready/files_published/manifest_published/recorded`；不能确认时为 `cleanup_pending/conflict`，没有发布为 `not_published`。

图片 rename 后普通读者可能已看见完整图片。manifest 的 replace 是分类发布点，图片、metadata 与 SQLite 没有跨存储原子性。恢复只核实原计划已发生的真实事实，允许补原收据；不重新请求模型、复制、提交旧分类、回滚或删除可能被主人采用的最终文件。仅身份仍准确归属原操作的私有 staging 可以清理。后续主人修改、根/代际变化或矛盾事实保留为 conflict，旧空间责任继续计量。

## 存储与图片预算

| 预算 | 约束 |
| --- | --- |
| 受管角色存储 | Drift 根、受管 memes、私有 stage/metadata、完整 journal UTF-8 数据与原目标/新副本峰值合计 64MiB、256 项 |
| 全局自主存储 | 原 1GiB 计量包含上述真实文件、保留预留及原逻辑记录费用 |
| 有界扫描 | 单根最多 512 项，最多 1024 个角色范围；超界、links 或未知归属拒绝新增写，不截断或删文件腾空间 |
| 原素材与 copy | 原图最多 10MiB、4000 万像素；每 run 最多 4 次 copy，复制原字节 |
| 视觉副本 | 长边最多 1568、像素最多 2,458,624、PNG/JPEG 最多 768KiB、base64 最多 1MiB |
| 实际图片曝光 | 每 run 最多 4 次、累计 base64 4MiB；同图重复、同 payload 多块、历史重带均逐次计量 |
| 图像输入估算 | 每曝光预留 8192 单位、每 run 32768；不是供应商 token/收费上限 |
| 文本与封装 | 原单次 48KiB、累计 512KiB；实际协议额外封装单次 8KiB、累计 160KiB；实际每请求图片 1MiB、非图片 56KiB |
| 原运行预算 | 20 步、输出 token 预留 8192、每请求 30 秒、原 tick 最多 600 秒；A01 原界不变 |

256 项按真实目录/文件项与一个共享 journal 容器责任计数；SQL 全部持久字段按 UTF-8 加原 256 字节/行政策开销计量。这是业务分配与逻辑记录预算，不是共享 `sessions.db` 物理文件大小硬上限。关闭后主人普通编辑退出新增 quota，旧未决责任仍保留在自主/global ledger。

图片预留在原 adapter 的实际 HTTPX 序列化 body 发送前落入原 run 的持久账本。`modelRequests` 区分 `protocol/bodyBytes/imageOccurrences/imageBase64Bytes/imageInputEstimate/wireNonImageBytes/envelopeBytes/bodySha256`。失败、取消、没有 usage 均不退款；未经计量的隐式重试不受支持。无此用途 context 时原 adapter 行为保留。

真实图片仅在 owned 当前请求内存中流转，下一请求后移除。transcript、工具文本、SSE、错误与后续历史只保存安全投影；模型正常回显中的已知图像、字典键与跨字符串片段也在持久化前投影。此机制不宣称识别任意未知编码或变换后的图像内容。

协作锁只约束同进程 Meido 的相关入口。任意同账号外部程序、OS 动作与多文件/SQLite 之间无原子 CAS；外部变化在可观察检查处拒绝或事后保留冲突责任。真实模型识图质量、账号与费用仍需独立具体批准。
