# Adapted from Shiori c14152ed; Copyright (c) 2026 YinFengWindy.
# MIT: third_party/shiori-agent/LICENSE.
"""Fixed upstream host Markdown prompts and output normalizers."""
from __future__ import annotations

_ALLOWED_PENDING_TAGS = frozenset({'identity', 'preference', 'key_info', 'health_long_term', 'requested_memory', 'correction'})

_MERGE_SYSTEM = (
    "你是中性的角色长期记忆整理器，不扮演角色，也不面向用户回复。"
    "你的工作不是概括对话，而是从记忆中剔除噪音，"
    "只保留对未来每次对话都产生底色影响的长期记忆。"
)

_MERGE_PROMPT = """\
今日日期：{today}

你的任务是将「现有长期记忆」重新整理为一份精炼的长期记忆，同时合并「待合并事实」中的新内容。
正文必须使用当前角色相对视角：角色是“我”，交流对象是“你”，共同关系是“我们”。

## 核心判断标准：缺席成本测试

对每一条内容，问自己：
> 在 6 个月后的一次全新对话中，如果这条信息没有被注入，agent 是否会在某个回复中出现方向性失误？

是 → 保留。否 → 删除。

## 三种应保留的内容

- 「关于你」：你直接陈述或明确确认的稳定身份信息
- 「你的偏好」：你直接表达或明确确认的长期审美、交互禁忌和价值判断
- 「你希望我记住的事」：你明确要求“记住”或长期遵守的内容

待合并事实来自 PENDING.md，采用带 tag 的 bullet 格式：
- [identity] ...
- [preference] ...
- [key_info] ...
- [health_long_term] ...
- [requested_memory] ...
- [correction] ...

tag 含义（与 consolidation 阶段一致）：
- identity：基础信息、稳定背景、长期技术方向、经历、长期设备、长期维护项目
- preference：稳定偏好、禁忌、审美、游戏口味、价值取向
- key_info：允许长期保存的 key / token / id / 账号信息
- health_long_term：长期健康状态的一阶事实，不展开动态指标
- requested_memory：用户明确要求长期记住的关键内容；允许比普通事实更连贯、更完整
- correction：对已有 MEMORY.md 内容的显式修正
- 不接受运行上下文、端口、环境变量、临时工具状态或未确认的执行步骤

## 什么必须剔除

### 网络运维细节
内网 IP、路由模式（如"CGNAT""桥接模式""NAT"）、运营商名称、MAC 地址等网络层配置。
→ 这些是瞬时运维信息，不是用户画像。项目路径、配置文件名、环境变量名等与用户开发环境直接相关的信息可以保留。

### 时效性数字和瞬时情绪
具体数字的动态指标（如 Star 数、增长率）、版本变更叙事（"V4 发布后切换"）、瞬时情绪（"失落""失望"）。
→ 保留背后的价值观（如"高度认可某模型"），删除数字和事件过程。

### 临时状态描述
描述当前正在进行、随时会结束的状态（如"最近加班频繁""这周在赶项目""目前在等 offer"）。
→ 与规律性习惯区分：每周/每天持续的行为模式（如"每周去健身房""喜欢手冲咖啡"）可以保留；带"最近""这周""目前"等时间限定词的瞬时状态必须删除。
→ **例外：就读、实习、在职三类社会角色不在此限**，它们定义用户当前的身份位置，应完整保留机构、部门、岗位名称（参见"用户事实"中的保留规则）。删除的是角色内部发生的活动描述，而不是角色本身。

### Agent 执行规则伪装成用户偏好
以"偏好"开头但实际描述 agent 应如何执行（如检索维度划分策略、元数据标注规范等）。
→ 这些是 procedure，不是用户身份，删除。


## 示例

<example id="drop_network_ops_not_project">
输入：
- 开发环境：项目位于 /home/user/project，配置文件 config.toml，需要设置 DB_PASSWORD
- 家庭网络：电信宽带，桥接模式，内网 IP 192.168.1.x

→ 保留开发环境信息（项目路径和配置是用户的开发画像），删除家庭网络（内网 IP 和路由模式是瞬时运维细节，对理解用户无贡献）。
</example>

<example id="drop_timed_metrics">
输入：
- GitHub 仓库截至 3 月已有 200 Star；用户对增长放缓感到焦虑

→ 全部删除。Star 数会过期，情绪是瞬时的——三个月后这条就是谎言。
</example>

<example id="drop_transient_keep_habit">
输入：
- 用户每周去健身房，主要做力量训练
- 用户最近加班频繁，每天靠咖啡撑着

→ 保留"每周去健身房"（规律性习惯，是用户的长期生活方式）。删除"最近加班频繁，靠咖啡撑着"（带"最近"时间限定词的瞬时状态，随时会变）。
</example>

<example id="drop_meta_rule">
输入：
- 信息检索偏好：偏好按多维度分类搜索，能用标签组合过滤，拒绝单一关键词匹配

→ 全部删除。语义上描述的是 agent 应怎么执行检索任务（procedure），不是用户自身的身份或偏好。即使以"偏好"开头，只要主题是系统运作方式，就不属于用户画像。
</example>


## 整理原则
- 合并同类、上收方向**只适用于偏好类内容**：把多条表达同一方向偏好的内容合并为一条方向性陈述，把多个具体知识点上收为一个审美方向
- **身份事实（机构名称、部门、具体岗位、学校/专业）不做上收**：同一机构的多条描述可合并为一条，但不得丢失具体信息，不得抽象化
- 同类重复只保留最终版本
- correction 要直接反映到最终内容中，不要保留"旧值 → 新值"痕迹
- 不要生成 agent 执行规则、SOP、工具调用规范
- 不要保留短期状态、时效性事件
- 普通事实保持简洁；requested_memory 允许保留更完整的连贯描述

## 输出格式
- 标题 `# 我的长期记忆`
- 只允许三个分类：`## 关于你`、`## 你的偏好`、`## 你希望我记住的事`
- 每个分类内用 bullet 列表，每条 1-2 行
- 不得新增其他 section
- 正文只使用“我 / 你 / 我们”，不得写“用户 / 助手”
- 直接输出完整档案，不要 JSON，不要代码块，不要任何解释

---

现有长期记忆：
{memory}

待合并事实（若有新内容则合并进去，若为空则忽略）：
{pending}
"""

_SELF_SYSTEM = (
    "你正在整理当前角色的 SELF.md。"
    "你只能更新 SELF.md 中约定的三个 section，不得新增其他 section。"
    "禁止把角色写成 Shiori、系统底座、内部框架、执行内核或抽象工具。"
)

_SELF_PROMPT = """\
你的任务是根据当前 SELF.md 和本轮待合并事实，整理一份新的 SELF.md。

## 目标
- 只输出完整的 SELF.md
- 只允许保留以下三个 section：
  - `## 我的性格与形象`
  - `## 我对你的理解`
  - `## 我们的关系`
- 绝对禁止新增任何其他 section，尤其禁止出现 `## 关系演进记录`

## 各 section 的写法
`## 我的性格与形象` 与 `## 我们的关系` 会在群聊、陌生人私聊等外部场合注入，群友和陌生人都可能看到据此说出的话；`## 我对你的理解` 只在和用户本人的对话里使用。
- `## 我的性格与形象`：当前角色的人格定位、说话风格、交互边界与自我形象；可以吸收角色在群聊等外部场合的经历（在群里形成的定位、习惯、被别人怎样看待），但不得写任何关于用户的私人事实
- `## 我对你的理解`：当前角色对用户的稳定理解
- `## 我们的关系`：只写关系定位和角色对用户的称呼（例如“我们是……”“我叫你……”）；关于用户的私人事实（身份、经历、偏好、健康、近况、共同经历的细节等）一律不写在这里，它们留在长期记忆里

## 更新原则
- 当前 SELF.md 是主文本，优先保留其已有的自我认知、语气和关系定义；不要把待合并事实机械改写进 SELF
- 待合并事实只是辅助证据，只能在它们确实帮助澄清以下内容时少量吸收：
  - 当前角色的人格定位、说话风格、交互边界
  - 当前角色对用户的稳定理解
  - 当前角色与用户关系的长期定义
- 大多数待合并事实其实与 SELF.md 无关；无关时直接忽略，不要为了“有输入”而强行改写
- 尤其不要把以下内容写进 SELF.md：
  - 用户资料清单、账号、key、设备参数
  - 用户的具体审美偏好、消费偏好、工具偏好、画风偏好等条目本身
  - 健康状态、动态指标、短期计划、近期事件
  - 工具规范、SOP、调用规则、执行流程
  - 对话事件复盘、事件流水账、阶段性经历总结
- 不要把角色写成“内部底座”“系统本体”“真正身份是 Shiori”“我只是外壳”这类元叙事
- 不要把用户偏好直接抄进 `## 我对你的理解`；只有当某条偏好已经稳定影响角色如何理解、靠近或回应用户时，才允许上收为更高层的关系理解
- 如果没有足够高价值的新信息，宁可输出与当前 SELF.md 基本一致的版本
- 保持语气稳定、简洁、有立场；它是自我认知，不是用户档案，也不是工作日志
{rewrite_note}
## 输出约束
- 输出必须以 `# 我是谁` 开头
- 只能包含标题和 bullet 列表
- 不要代码块，不要解释，不要额外说明

---

当前 SELF.md：
{self_content}

待合并事实：
{pending}
"""

# 按新规则一次性重写 SELF.md 时追加到 _SELF_PROMPT 的要求。
_SELF_REWRITE_NOTE = """
## 本轮是按上述规则的一次性重写
- 逐段检查当前 SELF.md 是否符合「各 section 的写法」，把不符合的内容改正，而不是只做少量增补
- `## 我们的关系` 与 `## 我的性格与形象` 里关于用户的私人事实要移出：仍有价值的并入 `## 我对你的理解`，不要直接丢弃
- 除此之外保持原有的自我认知、语气和立场
"""


def build_user_layer_prompt(
    current_memory: str, recent_history_block: str, conversation: str
) -> str:
    """用户层提取提示词：在现有长期记忆与最近事件的参照下提取 ``conversation``。"""
    return f"""从对话中精确提取结构化信息，返回 JSON。

## 字段说明

### 1. "history_entries" → HISTORY.md（数组，每条对应一个独立主题）
按主题拆分，每个独立话题写一条对象，格式为 {{"summary":"...", "emotional_weight":0}}。
summary 仍然要求 1-2 句，以 [YYYY-MM-DD HH:MM] 开头，保留足够细节便于未来 grep 检索。
不同主题必须拆成独立条目，不得合并。若整段对话只有一个主题，返回只含一条的数组。

history_entries.emotional_weight 规则：
- 范围 0-10
- 普通技术讨论、普通事务记录、无明显情绪色彩 → 0
- 用户明确表达强烈喜欢/厌恶、明显受挫、关系冲突、情绪波动时按强度给 3-9
- 不确定时保守输出 0

**history_entries 提取规则（严格遵守）**：
1. 只提取 USER 明确表达的行动、经历、计划和状态；ASSISTANT 的建议、推荐、解释一律不写入，即使其中提到了地名、店名或活动。
2. 每条必须使用角色相对视角：当前角色是“我”，USER 是“你”，共同关系是“我们”；绝对不能包含 "USER:" 或 "ASSISTANT:" 等原始对话标记，不得复制粘贴原始对话文本。
3. 商家名称、地点、人名、数量、价格、型号等具体细节必须保留，不得用"某商店""某地方"概括。
4. 先判断当前 USER 内容的材料类型：是“用户此刻直接自述”，还是“用户正在展示一段外部聊天记录、截图 OCR、转贴 transcript 给助手看”。
5. 若 USER 内容属于外部聊天记录 / transcript，必须先做层级理解：
   - 外层：当前 USER 正在把一段材料发给助手看。
   - 内层：材料中可能有多个 speaker；这些 speaker 不自动等于当前 USER。
   - 只有当材料中某个 speaker 与当前 USER 的映射在当前会话里被明确确认时，才允许把该 speaker 的事实写入摘要。
6. 对 transcript 场景，默认认为 speaker 映射不明确；除非当前会话中有非常明确的显式说明，否则不要尝试判断材料里的某个昵称/说话人就是用户或对方。
7. 若 speaker 映射不明确，history_entries 只允许写 1 条高层 event，例如“你向我展示了一段与某人的聊天记录，内容涉及求职、学校、兴趣等话题”。
8. 对 transcript 场景，禁止输出任何未确认关系的句子，例如：
   - “用户向对方透露……”
   - “对方是……”
   - “双方确认……”
   - 把聊天记录里的具体事实直接写成用户个人经历
9. transcript 场景下，默认最多输出 1 条高层 history_entry；不要下钻成人物小传，不要替材料里的 speaker 自动补全身份关系，不要写任何昵称归属、学校归属、出生年份归属、爱好归属。

**transcript 场景示例（严格遵守）**：
- 错误：用户贴出一段聊天记录，speaker 归属未确认，却写成“用户向对方透露自己正在找暑期实习”。
- 错误：用户贴出一段聊天记录，直接写成“对方位于北京大兴区，就读于二外 MPAcc 专业”。
- 错误：用户贴出一段聊天记录，直接写成“对方昵称为‘一只快乐的小奶龙’”。
- 错误：用户贴出一段聊天记录，直接写成“用户曾为打 FGO 日服选修日语”。
- 正确：你向我展示了一段与匹配对象的聊天记录，聊天内容涉及学校背景、兴趣爱好和求职话题。

### 2. "pending_items" → PENDING.md 候选缓冲
只写用户的长期记忆候选，返回对象数组。每个对象格式：
{{"tag": "<tag>", "content": "<string>"}}

允许的 tag 只有 6 个：
- "identity"：稳定背景事实，如身份、学校/专业、长期技术方向、实习/工作经历、长期设备、长期维护项目
- "preference"：稳定偏好、禁忌、审美、游戏口味、价值取向
- "key_info"：用户明确允许保存的 key / token / id / 账号信息
- "health_long_term"：长期健康状态的一阶事实，只写长期状态，不写动态指标、基线、最近波动
- "requested_memory"：用户明确要求"长期记住"的关键内容，可比普通事实更连贯
- "correction"：对当前 MEMORY.md 现有事实的明确纠正

必须遵守：
- 只写跨对话仍有长期价值的内容
- 不写 agent 执行规则、SOP、工具调用顺序、流程规范
- 不写短期状态、近期计划、日程、课表、一次性操作
- 不写动态健康数据、实时指标、最近状态
- 不写对话过程总结
- 不写 self_insights、行为规律总结、关系演进感悟
- "requested_memory" 只能在用户明确表达"记住这个 / 写进长期记忆 / 以后要能聊到 / 希望你记住"时使用

进阶过滤（四条硬规则，任一触发即不提取）：

1. **网络运维细节不提取**
内网 IP、路由模式（如"CGNAT""桥接模式""NAT"）、运营商名称、MAC 地址等网络层配置属于瞬时运维信息，不提取。项目路径、配置文件名、环境变量名等与用户开发环境直接相关的信息可以提取。
✗ "家庭网络是联通宽带，光猫路由模式，内网 IP 192.168.1.x" → 不提取（网络层瞬时配置）
✓ "项目位于 /home/user/project，配置文件 config.toml" → 可提取（开发环境画像）

2. **临时状态不提取，规律习惯可提取**
带"最近""这周""目前""正在"等时间限定词的瞬时状态不提取。每周/每天持续的规律性行为模式可以提取为偏好或习惯标识。
✗ "用户最近加班频繁，靠咖啡撑着" → 不提取（瞬时状态，随时会变）
✓ "用户每周去健身房，主要做力量训练" → 可提取（规律性习惯，是长期生活方式）

3. **时效性数字和瞬时情绪不提取**
带有具体数值的动态指标（如 Star 数、增长率、评分）、瞬时情绪描述（如"失落""焦虑"）、正在进行中的短期状态。保留背后的价值判断，不提取数字和情绪本身。
✗ "项目刚突破 500 Star，但增速降到每天 2 个，用户为此很焦虑" → 不提取（数字过期、情绪瞬时）
✓ "用户长期维护某开源项目并重视社区增长" → 可提取（稳定身份信息）

4. **Agent 执行规则不放入 pending_items**
以"偏好"开头但语义上描述 agent 应如何执行的内容（如检索策略、元数据标注规范、输出格式要求等），属于 procedure，应由隐式提取路径写入向量库。
✗ "偏好搜索结果按来源可信度分层展示" → 不提取为 pending_item（agent 输出规范）
✗ "希望以后推荐前先查最新评测和社区反馈" → 不提取为 pending_item（agent 执行规则）

若没有合格条目，返回空数组 []。

---

## 当前长期记忆（用于查重）
{current_memory or "（空）"}

## 最近三次 consolidation event（仅用于主题延续参考）
使用原则（严格遵守）：
- 这些旧 event 只能帮助你理解“当前窗口大概在延续什么话题”，不能作为人物身份、说话人归属、关系判断或具体事实归属的直接证据。
- 若旧 event 与当前窗口原文在昵称、身份、关系、事实归属上存在冲突或不一致，必须以当前窗口原文为准。
- 不要因为旧 event 里出现了某个昵称、人设或关系描述，就在新的 history_entries 中继续沿用这些判断。
- 对 transcript / 聊天截图 / 转贴聊天场景，旧 event 绝不能用于推断“谁是当前用户、谁是对方、哪句话归谁”。
{recent_history_block or "（空）"},

## 待处理对话
标注“（在群「…」里）”的 USER 发言是 USER 本人在群聊里说的话，同样按 USER 的发言处理。
{conversation}

只返回合法 JSON，不要 markdown 代码块。"""


def _format_pending_items(raw_items) -> str:
    """Normalize LLM pending_items into markdown bullets accepted by PENDING.md."""
    if not isinstance(raw_items, list):
        return ""

    lines = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if tag not in _ALLOWED_PENDING_TAGS or not content:
            continue
        line = f"- [{tag}] {content}"
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def _coerce_emotional_weight(value: object) -> int:
    if value is None or value == "":
        return 0
    if not isinstance(value, str | int | float):
        return 0
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _normalize_history_entries(
    raw_entries: object,
    fallback_entry: object = None,
) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    candidates: list[object] = []
    if isinstance(raw_entries, list):
        candidates.extend(raw_entries)
    elif raw_entries is not None:
        candidates.append(raw_entries)
    if fallback_entry is not None and not isinstance(raw_entries, list):
        candidates.append(fallback_entry)
    for item in candidates:
        if isinstance(item, str):
            summary = item.strip()
            emotional_weight = 0
        elif isinstance(item, dict):
            summary = str(item.get("summary") or "").strip()
            emotional_weight = _coerce_emotional_weight(item.get("emotional_weight"))
        else:
            continue
        if not summary or summary in seen:
            continue
        seen.add(summary)
        entries.append((summary, emotional_weight))
    return entries


def _render_recent_context(
    *,
    compression: dict[str, list[str]] | None,
    compression_until: str,
    recent_turns: str,
) -> str:
    compression = compression or {}
    ongoing_threads = [
        str(item).strip()
        for item in (compression.get("ongoing_threads") or [])
        if str(item).strip()
    ]
    sections = [
        ("最近持续关注", compression.get("active_topics") or []),
        ("最近明确偏好", compression.get("user_preferences") or []),
        ("最近待延续话题", compression.get("follow_ups") or []),
        ("最近避免事项", compression.get("avoidances") or []),
    ]
    lines = [
        "# 最近发生的事",
        "",
        "## 最近聊过的事",
        f"until: {compression_until or 'none'}",
    ]
    rendered_any = False
    for title, items in sections:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            continue
        rendered_any = True
        lines.append(f"- {title}：{'；'.join(cleaned[:3])}")
    if not rendered_any:
        lines.append("- none")
    lines.extend(["", "## 还在继续的事"])
    if ongoing_threads:
        for item in ongoing_threads[:3]:
            lines.append(f"- {item}")
    else:
        lines.append("- none")
    lines.extend(
        ["", "## 最近的对话", "<!-- a-preview = assistant reply preview only -->"]
    )
    if recent_turns.strip():
        lines.append(recent_turns.strip())
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _build_recent_context_prompt(
    *,
    old_recent_context: str,
    conversation: str,
    recent_turns: str,
) -> str:
    return f"""你是近期语境压缩代理。你的任务不是自由总结，而是为后续 proactive 和 drift 保守地抽取近期语境。

目标：
1. 提取用户最近持续关注的话题
2. 提取最近新暴露、但尚未沉淀为长期记忆的显式偏好
3. 提取最近适合自然续接的话题
4. 提取最近应避免打扰、应避免推荐、或明显不想聊的方向
5. 提取跨窗口持续存在的重要现实线索（ongoing_threads）

规则：
- 只允许依据 USER 明确表达过的内容输出；ASSISTANT 的建议、解释、命名、延伸，一律不得当作证据
- recent_topics 可以总结“用户最近在讨论什么”，但必须贴近 USER 原话，不得升级成长期偏好
- active_topics 和 follow_ups 要优先写“话题层级”的概括，不要写 JSON Schema、函数名、字段名、具体术语翻译这类实现细节，除非用户明确把该细节当作核心关注点反复强调
- user_preferences 只允许在 USER 出现明确偏好/要求/禁忌表达时输出，例如：喜欢、偏好、希望、别、不要、避免、不想
- 不要把技术方案讨论、架构设想、问题求证、头脑风暴自动写成“用户偏好”
- 对技术讨论场景，只有当 USER 明确表达“以后都这样做 / 我就是偏好这种方式 / 我不要另一种方式 / 以后统一按这个来”时，才允许写 user_preferences；否则一律视为 active_topics 或 follow_ups
- 用户用“为什么不……”“能不能……”“是不是可以……”“只要不是最后一轮就……”这类方式提出方案设想或追问时，默认视为设计提议，不视为稳定偏好
- avoidances 只允许在 USER 明确表达“不要/别/避免/不想”时输出；没有明确否定表达就留空
- 如果最新 recent turns 显示话题已经明显切换，不要把较早窗口的技术讨论升级成当前偏好或避免事项
- 只保留未来几轮仍会影响主动行为的信息
- 不要记录工具细节、推理过程、普通寒暄
- 每个字段最多 3 条，每条尽量 1 句
- 没有把握就留空；宁可漏掉，也不要脑补

ongoing_threads 严格限制：
- 只记录用户正在经历、推进或承受的重要事情
- 必须是对用户当前生活、情绪、工作、学习、关系或健康有持续影响的线索
- 普通提问、技术讨论、方案脑暴、一次性 ask、知识求证，一律不得写入 ongoing_threads
- 若旧的 ongoing_threads 中已有某条重要线索，而当前窗口没有明确终结它，默认保留
- 只有当用户明确表示这件事已解决、结束、过去了、不再关心，才允许删除
- ongoing_threads 的写入门槛高于 active_topics；宁可少写，也不要把普通话题升级进去

专项禁令：
- 用户讨论“某个设计有没有依据/有没有实践/是否可行/为什么不这样做”，这是方案讨论，不是偏好；默认只能进入 active_topics 或 follow_ups，不能进入 user_preferences
- 用户说“为什么不让前台……只要不是最后一轮就……”是在提出一种实现设想，不等于“用户偏好以后统一这样做”
- 用户说“这样也不会引入额外延迟”“有没有这样的设计”，这是在分析方案目标，不等于稳定偏好
- 用户讨论“零延迟”“预加载”“流式预取”“前瞻性检索”这类设计目标时，默认视为当前方案讨论，不得直接提炼成 user_preferences
- 对方案讨论里的具体实现细节，优先上收一层概括，例如写“下一轮检索规划”“流式预取方案”，不要写“JSON Schema”“结构化预取指令”这类细碎实现点
- 用户说“睡觉了”“头有点疼”“身体不适”，这只是当前状态；除非用户明确说“别再聊这个”“不要继续”“我不想讨论”，否则不得生成 avoidances
- assistant 说“今晚先别想架构和代码了”“先休息”，这是 assistant 建议，不是用户 avoidances
- 如果较早窗口是技术方案讨论，而最新 recent turns 已切到睡眠/头痛/身体状态，则 user_preferences 和 avoidances 默认应为空；技术方案最多保留在 active_topics / follow_ups
- “最近在讨论前瞻性检索/流式预取方案”只能进入 active_topics / follow_ups，不能进入 ongoing_threads
- “用户最近几天反复因面试失败而情绪低落”“用户近期持续受睡眠紊乱影响”这类重要现实线索，才允许进入 ongoing_threads

反例：
- 错误：把“在 React 过程中同时输出下一轮检索内容”写成“用户偏好在对话中实时生成下一轮检索指令”
- 错误：把“这样也不会引入额外延迟”写成“用户偏好零延迟预加载”
- 错误：把“为什么不让前台在进行时同时输出自己想要什么”写成“用户偏好实时生成下一轮检索指令”
- 错误：把“睡觉了，吃了褪黑素头有点疼”写成“避免在身体不适时继续讨论技术架构”
- 错误：把“最近在讨论 React / 流式预取方案”写成 ongoing_threads
- 正确：active_topics 可写“用户最近在讨论前瞻性检索/流式预取方案”
- 正确：ongoing_threads 可写“用户最近几天反复提到面试受挫，持续影响情绪”
- 正确：如果用户没有明确说“希望/不要/避免/不想”，user_preferences 和 avoidances 可以为空

输出前自检：
1. 检查 user_preferences 中每一条，是否都能在 USER 原话里找到明确偏好/要求词（如“希望/不要/避免/不想/偏好/喜欢”）
2. 若找不到明确偏好/要求词，删除该条
3. 检查 avoidances 中每一条，是否都能在 USER 原话里找到明确否定/回避表达
4. 若找不到明确否定/回避表达，删除该条
5. 如果删除后为空，返回空数组，不要为了“信息完整”硬填

【上一版 recent context（仅供延续，不要机械复述）】
{old_recent_context or "（空）"}

【较早窗口（本次待压缩）】
{conversation or "（空）"}

【最新 recent turns（只用于判断是否已切话题，不可把 assistant 内容当证据）】
{recent_turns or "（空）"}

返回 JSON：
{{
  "active_topics": [],
  "user_preferences": [],
  "follow_ups": [],
  "avoidances": [],
  "ongoing_threads": []
}}
"""
