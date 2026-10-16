# Drift 内置表情库维护规格

状态：草案 / 具体架构已完成独立规范与需求设计复核，无剩余设计发现；第3节待主人明确批准，尚未写生产代码。最后核验：2026-10-17。

Parent：[总目标 #137](https://github.com/sawaki-zexi/meido/issues/137)；实施票：[Drift表情库维护 #191](https://github.com/sawaki-zexi/meido/issues/191)。统一交付[总草稿PR #162](https://github.com/sawaki-zexi/meido/pull/162)，不另建功能PR。

事实来源：[总规格](shiori-tool-parity-spec.md)、[Drift规格](proactive-drift-spec.md)、[S04正文](../../backend/app/builtin_skills/meme-manage/SKILL.md)、[普通媒体链验收](../research/meme-skill-media-acceptance-2026-10-16.md)、[协作规则](../../AGENTS.md)。源码基点为准确双CI通过的`6751bcc425ad1c0fc4823691fbdda155a89c7bdf`。设计依据为外部固定v3 SHA256 `c8f78feffc61e7d5dd030c8edc8746a94d4053cdfe3bceaa6e905b7d5cac595d`及最终两轴报告；新增取锁说明只澄清等待预算，不增加OS耗时保证。

本次拟批准范围仅为实现、隔离构建与无费用本地合成验收；真实视觉服务、账号/来源/平台事务、凭据、模型消费及发布仍须具体另批。本文不是批准记录或现状实现证明。

## 1. 问题与证据

总规格保留完整76工具、8Skill及A01/A02，不允许把永久不可用占位视作复刻。固定Shiori `4f266ae`的agent_tick_factory.py:33/255–257实际纳入Drift builtin meme-manage；其drift_turn.py:312/529仍把工具输出转字符串/纯文本，不能据此声称上游完成真实视觉核验。

Meido普通S04已有主人上传→真实表情库复制→read_file图片进下一provider→原登记ID复用的直接证据。Drift的drift_skills.py:353–359则无条件pending_dependency，:418拒绝激活，builtin_author测试:22–23固化该拒绝；drift.py:76关闭multimodal，:457只追加文本结果。阶段规格§9披露此差距，没有删除原总目标。

原已批准proactive-drift§3/Issue179约束Drift私有工作根及只读资源、有限Shell/MCP和原Runtime；没有明确批准自主写共享memes、素材图送Drift模型、新图片预算及窄复制。这些新增跨功能边界须在具体方案复核后一次批准；不能删pending或放开Shell冒充完成。#152是普通聊天S04，#179是S06/通用Drift，由Parent137的独立纵切#191补齐，不重复实现两票。

## 2. 用户结果与范围

主人独立许可后，实际内建meme-manage能在合格Drift中查看真实素材、维护当前角色表情库类别/描述/别名，并按许可复制已登记素材。静默finish、工作进度与nextAction持久；完整重建后下一tick实际读取并继续维护同一产物。维护本身不自动发送。

既有旧库只有`memes/manifest.json`与图片时，只要明确库读许可和实际路径/图片校验通过，Drift也能查看图片、维护类别；不要求先上传、补publicId或补新metadata才算合法库。旧图片没有附件ID就不能发送。新复制只从本run原已冻结的当前角色上传ID取源，记录原ID与实际副本身份；不创建任意库路径发布接口，也不改变原附件的路径/归属。

普通聊天S04仍用原文件/图片/复制命令权限及原Skill工具声明；其他Drift Skill不获得共享库能力，其他七份普通Skill不因本开关自动进入Drift。现有S06与#180发送/恢复合同保持。本profile不新增message_push声明，S04以silent完成；独立可选投递仅在已有具体模式声明及原run附件/目标grant均允许时复用原public ID，不是本纵切验收前置。

## 3. 待批准的具体架构

### 3.1 用途许可、身份与显式视觉绑定

新增角色`driftMeme`有限用途配置，默认关闭，分开列库读/分类编辑/复制许可、有限类别和输入附件ID范围。它绑定当前角色、固定库根身份、配置revision及主人明确选定作视觉用途的已保存Drift模型ID；该ID必须与本run明确Drift模型一致。不开第二个隐式视觉助手，不在激活后换模型，不取chat/active/screen/另一角色fallback；不支持图像或绑定不一致时给准确不可用原因。

每tick冻结原AutonomyOrigin、配置/模型及grant代际、固定builtin bundle/hash、库root/祖先身份和允许源集合。新主人输入、撤权、目录/模型/配置变化seal旧代际；恢复开关不能复活旧run。目录查询不读图或访问模型。执行前、图片发出前、复制与分类发布前再次重验；同名role/S06、伪builtin、未激活和成功发送后的读图/复制均由实际dispatch拒绝。

普通文件动作取角色read/list/write/edit许可∩Drift文件grant∩模式声明∩meme用途许可。新增窄`meme_copy_image`取明确`copyEnabled`∩固定模式声明∩原run源/库/角色授权，且须相应源读取/库写入文件许可；不能要求不存在于普通76工具目录的copy名加入enabledTools。Shell开关、白名单和只读preset均不参与此复制许可。

### 3.2 固定builtin的模式声明

版本化S04 builtin，并附经支持文件hash核验的builtin专用Drift profile；不改普通通用frontmatter parser。普通聊天继续原read_file/list_dir/write_file/edit_file/shell声明。Drift有效工具为read/list/write/edit/窄copy及既有finish，不含shell；配套固定指令明确库地址/图片读取/复制流程。

仅实际固定builtin来源、版本及完整SKILL.md+profile bundle hash匹配时，discovery、依赖检查、activate、schema和dispatch共同使用该有效声明。不得仅删schema里的Shell但仍按原frontmatter判缺依赖。自定义S06或本地同名覆盖仍是自己的普通Drift声明，不能继承该profile/共享写库；模型自报source/hash/profile不被采信。

### 3.3 窄库接缝与真实文件操作

一个窄角色表情库Module拥有库观察版本、CAS、复制及恢复；复用现有FileTools路径/祖先检查、同角色规范path锁、图像解码、原子单文件replace和owned取消join。普通Drift相对路径仍在私有根；共享库使用明确地址空间，仅映射固定当前角色`workspace/memes`。不把整个角色workspace开放给Drift，不接任意绝对根、URL、base64、命令或env。

read/list在真实内容之外返回宿主不透明库版本，绑定root/文件身份、hash和revision；宿主也按原role/run/path保存实际观察。针对受管memes的主人普通FileTools与Drift写都参加同一短期path锁/版本协议：读后更新沿原观察CAS；已有目标缺有效观察先返回needs_read，创建用实际不存在证明，不把当前最新文件悄悄充作旧预期。普通工具权限和非库文件语义不改；这项窄并发合同须在UI/工具错误里解释，不能只为Drift新服务加锁而忽略旧写入口。

**受管状态的进入/退出。** 从未开启本用途的角色保持unmanaged：普通聊天S04、原FileTools权限/写入合同及原数据预算完全不变，不因新模块存在增加needs_read/CAS/64MiB限制。只有主人显式启用或采用此角色固定库，才分配managedEpoch并让该库的主人FileTools与Drift共同进入上段CAS/合计预算策略；这是本次待批的真实兼容取舍，不把“已有64MiB”说成原来已约束所有memes。

关闭用途先在原角色同步门seal旧代际并撤销新受管准入，状态可为disabled_draining；立即禁止旧模型/工具/文件worker获得新的发布资格，并异步cancel/join原资源。主人不等待未知模型/业务清理：禁用后的普通编辑恢复原S04/FileTools合同；若恰逢原短发布临界段，仅允许有界path-busy反馈并重试（建议获取等待≤250ms），不能把整个旧run或unknown journal变成主人库锁。原worker自然join后其后续普通编辑当然也不再受新needs_read/CAS/合计quota限制；重新启用须等待旧owned资源退出，并创建新epoch，旧run不复活。

250ms仅是每次取锁等待预算；超时返回`path_busy`由调用方重试，不是rename/fsync或普通写完成时限。已经取得发布资格的OS动作仍保留原责任并须自然join；不得以超时或停用假报释放，也不得在owner后续写入后晚到覆盖。

为使上述语义成立，受管Module的长扫描、图片预处理、staging写/fsync、网络与恢复等待都不得持发布path锁；复用FileTools原单文件准备/replace接缝，将该库的最终版本/授权检查与rename放在短路径临界段，seal与最终发布资格使用同一同步门。禁用后普通FileTools与仍活旧worker仍共享路径次序，但旧epoch只能收尾已发生事实，不能在owner新写后晚到replace。不能为提早返回而假报worker已join；真正无法确认的资源继续拥有并阻止新的自主run，不能转化为对主人编辑的长期禁止。

旧journal/预留只绑定原操作继续只读核实或安全私有staging清理，不授权新库写，不延续needs_read/CAS/quota到后续普通S04。若关闭后主人已写入新版本，恢复记conflict并保留其字节，不覆盖、不重复制/模型，也不删除可能采用的最终文件。旧未决空间仍计入自主业务责任/global ledger，不能清账冒释放；这与主人普通编辑是否受新用途策略是两件事。重建应用按持久enabled/managedEpoch决定当前入口，不能因旧journal存在自动恢复managed状态。

窄copy只接本run冻结的当前角色源attachment ID、获准类别和原请求身份；宿主分配新文件名，不覆盖已有图片。真实解码后复制原字节，记录源public ID、实际副本路径/身份/hash及原操作；既有旧库不因缺该记录失去读图/分类能力。源/副本一致性与视觉压缩副本分别核验。复用原public ID只保留已有发送可能，不授予新发送资格。

### 3.4 旧版本CAS、可见阶段与恢复

首次持久意图固定原role/run/call/builtin/grant、root/祖先身份、库revision、manifest及将触及旧文件的expected身份/hash或准确不存在、源ID/hash、宿主目标名、完整planned图片/metadata/desired manifest内容及hash、有限预留。不能恢复时重新让模型决定内容或把最新owner版本改成expected。多路径按固定顺序一次取得既有path锁，避免重复拿非重入锁；不跨模型/网络持锁。

当前manifest只描述categories，不逐图列引用，不能虚构“图片被manifest隐藏”。新图片及库metadata先在私有staging完整写/fsync/校验，再各自原子rename；一旦图片进入类别目录，旧普通list/read已可能看到完整新文件。manifest原子replace是分类发布点，并非所有图片/metadata/SQLite的物理原子点。发布分类前在同路径锁中CAS原观察/库revision和当前授权；保留原有类别及图片。禁止manifest或成功收据宣称未完成的计划。

持久阶段至少区分reserved、files_ready、files_published、manifest_published、recorded、conflict/cleanup_pending；各阶段保存实际路径/身份/hash，文件可见阶段不能被SQL失败或取消抹掉。业务成功必须核验本操作所有planned文件/metadata/分类事实及对应记录；尚未确认给pending/conflict，不能用单个文件存在或SQL成功假装整体完成。

恢复只核对原意图：已发生且仍精确吻合的发布只补原事实/收据；manifest未发布不重做模型/复制或提交旧编辑。主人普通FileTools后来改变manifest、图片、metadata，库revision/root身份改变或事实矛盾，都保留主人新版本并记conflict/责任，不自动覆盖、回滚或合并。只清理归属/身份仍可证明的私有staging；已落最终目录且可能被主人采用的文件不自动删。未确认责任/配额保持，满时阻止新自主写。

协作锁只覆盖同进程Meido FileTools/本Module，不约束任意同账号OS程序。跨FS/SQLite无原子事务，校验与replace之间也非OS多文件CAS；外部编辑/ABA在可观察检查处拒绝或事后conflict，不宣称完全拦截。读者可看到完整但未完成整项维护的物理阶段，实际阶段与nextAction必须可诊断。

### 3.5 实际存储预算

现DriftFiles只扫描`workspace/drift`，不能口头声称新memes已被计量。实现必须把当前角色`memes`与该Module固定的私有staging/metadata/journal根加入有界扫描/分配账本，并让实际retention总额读取它们；与原drift根不重叠计数。复用既有allocation gate及文件write_policy接缝，不新建通用文件系统。

建议原角色自主64MiB/256项改为**Drift根+受管memes+本Module私有stage/metadata的合计峰值**，全局原1GiB也包含这些实际计数；旧库/未知文件、目录项、原目标、临时新副本及保留journal都计入。源上传位于已有附件库，只读引用不重复计存储；一旦实际复制，新增每份及峰值都计入。仅显式启用/采用后处于managed的主人库写也经过该分配策略；unmanaged及关闭后的普通编辑不进入此新增quota。超有界扫描/links/未知归属或旧库已超限时拒绝新自主写并说明真实原因，不截断扫描后假报空间足够，不删除owner文件腾空间。用途关闭后的旧责任仍计量并恢复，但不因这些旧责任永久限制普通S04；只读旧库和普通文件权限不靠清journal恢复。

单份复制图≤10MiB，分类文本沿原FileTools有限文本界；最多4次copy/run。quota在写任何staging前预留实际峰值，文件/metadata发布失败或取消后仅根据实际事实结算；未知/冲突保留有限责任。空间及entry满额的完整反例须使用真实文件，验证新写被拒且owner库字节不变。

### 3.6 图像数据、逐次预留与审计

主方案保留直接可见图片合同：read_file输出受管视觉副本，实际本轮明确Drift provider下一请求收到ModelImageBlock，随后像普通loop的ephemeral图片一样移除。不是独立助手给文字摘要。原图与视觉块不入transcript、tool输出/历史、预算日志、异常trace、SSE或provider原始回显；仅保留安全ID/hash/尺寸/字节/阶段及模型ID。当前turn.py:170–173持久完整payload，必须拆实际请求与安全审计投影，不能只设multimodal=True。

原图≤10MiB/40M解码像素；视觉副本长边≤1568、≤2,458,624像素，实际PNG/JPEG≤768KiB、base64≤1MiB。拟增独立图片配额：每run最多4次实际图片曝光、每个payload每个image块都计1，累计base64≤4MiB；不按唯一ID/read_file次数去重。同图重读、历史重带、任何重试都逐次计数和字节；受管复制次数与模型图片曝光分开。

文本/schema/身份仍保持原48KiB单次、512KiB累计；仅已验证图片块的base64可用独立配额，不能把任意工具文本伪装成图片豁免。具体协议额外JSON/字段封装另限8KiB/请求；实际UTF-8请求体总界为1MiB图片+56KiB非图像/封装，run额外封装累计≤160KiB（原最多20步）。每个实际provider序列化payload发送前统计/预留全部image出现次数、实际编码字节和实际完整body；任一界超限均出网前拒绝，不能只量上层唯一图片集合。复用已有adapter请求构建/发送接缝做本用途计量，不另写HTTP gateway；以真实localhost outbound payload核对各协议映射和账本。

每次实际图像曝光另预留8192个图像输入token**估算单位**，run最多32768；非供应商输入token/费用硬上限。具体图像计数策略及usage边界须可解释，不支持时明确预算不支持。每次provider调用仍占原20步、8192输出token预留、30秒及原600秒deadline；A01的12288输出token界不变。预留后失败/取消/usage缺失不退款。原adapters没有已证明的重试bug；约束是若未来SDK/transport有隐式重试，须逐次发送前计账或禁用该重试，否则拒绝此图像能力。

取舍是允许常规尺寸真实素材，增加有限图片内存/输入成本，并明确新增预算批准；不是仅极小PNG能挤过旧48KiB。实际文本/图片计量、估算单位与真实usage分栏，不能把估算单元冒充精确收费。缩放/解码由原run owned线程/进程拥有，取消一次并join，准入不早于全部旧资源退出。

## 4. 最小完整验收与交付

只在三个新MEIDO根、全程localhost guard、临时角色和自建图片/旧库中验收，不读真实素材、凭据或模型。fake provider证明字节/协议而非识图质量；真实视觉/费用以后按具体范围另批。

1. **Shell-off闭环与旧库兼容。** 真实HTTP保存默认off用途/模型/库许可；Shell关闭、白名单空、无Shell preset时，实际固定builtin profile可发现/激活，schema有copy无shell。已有无publicId/metadata的常规尺寸旧库图实际进入下一provider，真实类别/别名维护成功，silent finish/nextAction持久；没有伪造发送ID。
2. **真实复制与后续采用。** 主人HTTP上传自建常规图片并明确源ID许可，实际时钟进入Drift；模型只从真实消息取ID，执行窄copy、源/副本hash一致、真实库metadata与manifest验证。完整关闭重建应用/Runtime/SessionStore/相关owner后，下一tick从同库实际读图并继续修改类别；普通聊天随后也读同一真实产物。只文件存在不算跨tick消费。
3. **共享并发与各阶段重启。** 主人普通FileTools写入插在Drift读后发布前，以及崩溃后恢复前，必须保留主人新类别/图片且原意图conflict；copy/staging/metadata/图片rename/manifest replace/SQL各切点分别记录真实阶段。恢复不重模型/复制/发送，不覆盖后来版本，不删除owner文件；最终只有完整原提交或准确partial/conflict责任。
4. **每次图片曝光实际计账。** 真实localhost请求记录同ID在两个payload出现扣两次、同payload两image块扣两次、历史重带/重试也扣或被禁止；额度不足发送前拒绝，失败/取消/usage缺失不退预留。验证常规尺寸图/压缩、逐次byte/wire/pixel/估算及原文本/步/token/deadline边界，扫描全部正常和错误持久面无原图/base64。
5. **权限、配额与收尾。** 伪builtin/role同名/S06/未激活/错误源和类别/其他角色/路径link与rootABA/配置及模型换代拒绝；新主人输入和重复取消等待原provider finally、图像worker、FileTools/复制退出，下一自主准入等待owned join。真实旧库/新临时副本/journal令64MiB、256项、全局1GiB满额时不新写、不删owner文件。普通S04/S06及#180原协议回归保留。

6. **受管启停与owner继续编辑。** 默认从未启用角色的普通S04完全按原合同；显式enable/adopt才出现新CAS/合计quota。持有真实慢provider或staging-fileworker后disable，owner普通FileTools立即能继续（仅真实短path临界段可有界busy重试），不等旧run/未知清理；旧worker释放后不得覆盖新owner字节。保留未决journal/预留并重建应用，owner仍可按原合同编辑，恢复只读核实/安全私有cleanup且新版本conflict；re-enable生成新epoch且等真实owned退出，不复活旧run。

本规格正文依据v3及两轴最终复核的取锁措辞形成，明确新增第3节获主人批准后才写生产；实施TDD及必要准确HEAD回归/构建/CI，统一总PR162，不建堆叠PR、不自行合并或关闭Issue。原Spec/Standards报告中的3个独立设计缺口（模式声明、逐payload计量、共享CAS/恢复）不能仅凭作者本稿宣称复核关闭。
