# Drift 表情维护的本地验收

状态：当前功能分支验收流程；最终冻结结果见[实证记录](../research/drift-meme-maintenance-acceptance-2026-10-17.md)。事实来源：[批准规格 §4](../specs/drift-meme-maintenance-spec.md)、[配置与错误契约](../reference/drift-meme-maintenance.md)、[HTTP 验收](../../tests/python/test_drift_meme_api.py)、[跨进程夹具](../../tests/fixtures/drift_meme_rebuild.py)、[真实协议预算验收](../../tests/python/test_model_image_request_budget.py)。最后核验：2026-10-17。

## 准备隔离环境

在任何应用 import 或 pytest collection 前设置四个全新绝对根：`MEIDO_DATA_DIR`、`MEIDO_ROLES_DIR`、`MEIDO_OWNER_KNOWLEDGE_DATA_DIR`、`MEIDO_BROWSER_USE_DATA_DIR`，另用新 HOME、USERPROFILE、TMP/TEMP 和项目根。关闭无关后台记忆；清继承的服务、代理、CLI 和凭据配置。测试的 OS vault seam 只返回空 get、拒绝 set，不读取真实服务值。

整个原 owner 生命周期保持 localhost 守卫。App/TestClient、TaskRuntime、原主循环的 memory/channel closers、模型 finally、图像与文件 worker、子进程 Windows Job、读写管道与线程均自然收尾后再恢复守卫。退出码或超时本身不证明这些资源已结束。慢操作夹具的 watchdog 只释放夹具自己的 Event，不代替生产取消或 join。

本地用自制 PNG、临时上传与 HTTP/SSE provider。它们验证真实字节、计量和权限，不能证明远程模型理解图片或真实平台投递质量。无需开 Shell、安装 CLI、UIAccess、AppContainer 或其他 OS 策略。

## 验收一个完整用户结果

1. HTTP 创建临时角色并关闭 Shell；保存 Drift 明确模型和用途许可。确认默认 off、GET 回显宿主代际、内建 S04 可发现/激活、有效 schema 有窄 copy 且没有 Shell。检查 authoring 关闭时此独立用途仍能运行。
2. 直接放入没有附件 ID/metadata 的常规尺寸旧库图和 manifest。时钟实际启动 tick；检查下一模型请求有真正图片、类别/别名变化、其他类别字节保留、silent finish 与 nextAction 持久。
3. HTTP 上传自制图片并明确授权原素材 ID。模型从真实宿主消息取得 ID，执行 copy；检查源与副本字节/hash、metadata、manifest 和完整操作收据。关闭整套应用与相关 owner，第二个真实进程恢复后触发下一 tick，实际读同一新副本继续维护；普通主人对话也实际读同一图片。
4. 在真实 provider serializer 与 localhost socket 之间观察原持久账本。重复图片、同 payload 两图、历史重带分别检查每次出现计量；原额度不足时服务器不得收到下一个请求。检查协议封装、真实完整 body hash/字节、估算单位与 usage 分栏，不按唯一 ID 去重或退款。
5. 在实际 fsync、hash、解码、resize 与 provider finally 持有原资源，再通过 HTTP 停用、新主人输入和重复 stop。主人实际编辑继续，新自主 run/重新启用等待原 owned join；释放后旧 worker 不能覆盖新文件。
6. 在图片、metadata、manifest rename 与 SQL 原切点失效，核实可见的物理阶段。保留未决 journal 后停用、完整重建，再让主人普通编辑和读取同副本。恢复只能核实/清理原私有 stage，不能重做模型、复制、覆盖或删除主人产物。
7. 用真实文件分别填满角色字节、项数和全局空间，包含旧库、未知文件、新临时副本及完整 journal JSON 转义字节。确认写任何 staging 前拒绝、原文件保留。不要使用稀疏文件或计划摘要代替真实空间反例。
8. 分别核对未激活、S06、已采用的同名 role 技能、伪 builtin/hash 变更、错误素材/类别、其他角色、真实 link、root ABA 与模型换代；检查无新图、copy 或发布资格。随后跑普通 S04/S06、FileTools 和原主动投递/恢复合同回归。

## 记录结果

每次保留执行前后源码/资源 SHA、准确 argv、清理过的环境名字、原日志/XML、vault 次数、网络守卫与原 owner 终态。反例要区分产品失败、夹具装配失败与人为中断；未确认清理的尝试不得归入成功或有效 RED。跨进程记录每个真实 PID、原 Job/EOF/handle 终态与各 phase 的原 closers，不能只保存父 pytest 的绿色摘要。

类型检查使用准确基线完整 backend 归档、同一组既有源与 flags，再加入新增源，保存全部诊断和行映射。旧诊断未消除时只报告新增数量，不称全类型通过。支持文件使用固定 LF 字节；Windows 新 checkout 必须实际重验 SKILL/profile hash，不能用 `git show` 或 archive 代替 checkout 的换行验证。

协作路径锁与根/身份检查不构成同账号 OS 安全沙箱，也不提供多文件/SQLite 物理原子事务。真实服务、账号、素材和模型费用的验收需要另行明确范围；本流程不得据本地合成成功勾掉这些限制。
