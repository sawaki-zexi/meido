# NovelAI 完整生图工作流的移植差距

状态：草案 / 源码调研与 D05 待批准方案；P06、T19 尚未实现或验收。

最后核验：2026-10-16。事实来源：Shiori-Agent 固定提交 `4f266aeaf42fcd611362e2456417b91f659341cf`；Meido 聚合分支核验时 `c03a9fbdd2450e4df5171f7272c4abbd0e4258b4`；下列 NovelAI 官方公开文档。目标为[总规格 P06、D05、T19](../specs/shiori-tool-parity-spec.md)。以下明确区分已提取事实、静态推断和未实施建议。

本轮仅阅读本地源码与公开文档，包括公开 Swagger JSON；未读取真实 token、账号资料、系统 vault 或用户认证配置，未访问生成接口、安装依赖、启动插件或产生付费请求。没有真实生成成功、当前账号资格、Anlas 消耗或取消退款的实验证据。

## 已提取：范围、依赖与许可证

P06 不止一个工具名称：txt2img、img2img、模型/尺寸/seed、归属媒体、随回复展示且不重复、历史、重生成、自动场景 CG 都在范围内；健康检查不允许隐式生图。[规格 L52](../specs/shiori-tool-parity-spec.md#L52)、[D05 L135–139](../specs/shiori-tool-parity-spec.md#L135)、[验收 L206](../specs/shiori-tool-parity-spec.md#L206)、[T19 L247](../specs/shiori-tool-parity-spec.md#L247)、[完整范围 L265](../specs/shiori-tool-parity-spec.md#L265)。

固定插件默认停用，版本为 0.1.1；Python 依赖是 `shiori-sdk>=3.1.1,<4` 和 Pydantic，没有引入 NovelAI 官方 SDK。`NovelAIClient` 是插件自己的 HTTP 适配器，`shiori-sdk` 是 Shiori 的宿主 SDK，不能把它标成 NovelAI 官方 SDK。[manifest](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/manifest.yaml)、[pyproject](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/pyproject.toml)、[client.py L11–42](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/client.py#L11-L42)。

仓库根许可证为 MIT，署名 `Copyright (c) 2026 YinFengWindy`；本轮没有找到 NovelAI 插件另列的许可证。适配源码须保留 MIT 版权及许可声明，不能将其解释为 NovelAI 托管服务、模型或第三方图片的开放许可。[固定 LICENSE](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/LICENSE)。NovelAI 服务条款另行适用；其内容归属条款不等于保证任何生成内容均无第三方权利限制。[NovelAI Terms](https://novelai.net/terms)。

## 已提取：工具精确输入与实际校验

工具名为 `generate_image`。下面完整保留固定源码的 `parameters`，包括描述与约束；没有声明 `additionalProperties: false`，也没有 schema 级 img2img 底图或 scene CG 条件必填约束。[tool.py L16–100](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/tool.py#L16-L100)。

```json
{
  "type": "object",
  "properties": {
    "prompt": {"type": "string", "description": "逗号分隔的英文 NovelAI 正向 tags。禁止中文和自然语言句子；用户使用中文描述时，先在内部转换为英文 tags。"},
    "mode": {"type": "string", "enum": ["txt2img", "img2img"], "description": "生成模式：txt2img 为文生图，img2img 为图生图。"},
    "base_image_path": {"type": "string", "description": "img2img 模式下的输入图片路径。"},
    "negative_prompt": {"type": "string", "description": "逗号分隔的英文 NovelAI 负向 tags，可选；禁止中文。"},
    "strength": {"type": "number", "description": "img2img 强度，范围 0 到 1。", "minimum": 0, "maximum": 1},
    "noise": {"type": "number", "description": "img2img 噪声，范围 0 到 1。", "minimum": 0, "maximum": 1},
    "size_preset": {"type": "string", "enum": ["square", "landscape", "portrait", "custom"], "description": "尺寸预设。"},
    "custom_width": {"type": "integer", "description": "自定义宽度，仅 size_preset=custom 时使用。", "minimum": 1},
    "custom_height": {"type": "integer", "description": "自定义高度，仅 size_preset=custom 时使用。", "minimum": 1},
    "steps": {"type": "integer", "description": "采样步数。", "minimum": 1},
    "seed": {"type": "integer", "description": "随机种子，可选。"},
    "sampler": {"type": "string", "description": "采样器名称，可选。"},
    "model": {"type": "string", "description": "模型名，默认使用全局配置。"},
    "intent": {"type": "string", "enum": ["user_requested", "scene_cg"], "description": "调用意图：用户主动要求生图，或角色为关键场景生成 CG。"},
    "scene_key": {"type": "string", "description": "自动场景 CG 的简短场景标识；intent=scene_cg 时必填。"}
  },
  "required": ["prompt", "mode"]
}
```

执行时另检 intent、scene CG 的非空 scene_key；role_id/session_key 不在工具 schema，由宿主执行参数提供。源码的这种接入约定不能替代 Meido 的 ToolContext 强制归属。prompt 检查实际只验证 ASCII，未验证描述承诺的“逗号 tags、禁止自然语言”语法。[tool.py L111–165](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/tool.py#L111-L165)、[prompt_validation.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/prompt_validation.py)。

| 项目 | 固定服务实际行为与移植差距 |
| --- | --- |
| 尺寸与步数 | square=1024×1024、landscape=1216×832、portrait=832×1216；custom 每边正整数且 ≤1024；所有模式面积 ≤max_pixels，默认 1,048,576。steps 默认 max_steps=28，须在上限内；未见 64 倍数校验。API 的 int 转换与 tool schema 要统一，禁止布尔、NaN/Infinity 等隐式接受。 |
| 模型 | 默认 curated，另配 full。显式 model 只须等于两者之一；即使 nsfw_enabled=false，显式 full 仍能通过。该开关实际选择默认模型，不是完整模型授权边界。Meido 应改为主人批准的明确 allowedModels。 |
| img2img | base_image_path 经 SDK resolve_path 限于宿主 workspace 后读全文件，支持 PNG/JPEG/GIF/BMP/WebP；并非任意主机路径，但没有当前角色/原来源附件授权或此处的字节上限。strength 实际 .01–1，noise 0–.99，与 schema 的 0–1 不完全一致。 |
| seed/sampler | seed 给出才发；未给时记录可能仍为 null，没有取回实际 seed 的完整流程。tool 默认 sampler=k_euler_ancestral，而 RPC 默认 k_euler。不能以相同 seed 代替消费幂等。 |
| reference | img2img 底图是实际参考媒体；v4.5 请求构造中 reference_*_multiple 数组为空，没有工具输入可配置 Vibe/人物参考。不能将这些空数组宣称为已支持额外参考能力。 |

证据：[service.py L320–458](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L320-L458)、[SDK 路径限制 L6–24](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/files/paths.py#L6-L24)、[tool.py L126–154](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/tool.py#L126-L154)、[rpc.py L44–79](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/rpc.py#L44-L79)。

## 已提取：配置、请求、响应与持久化

| 配置 | 固定默认值 / 用途 |
| --- | --- |
| token | 空；生成请求 Bearer 鉴权，也用于默认模型 HTTP500 后的账号诊断 GET。空值或未展开 `${VAR}` 在出网前视为未配置。 |
| base_url | https://image.novelai.net；拼 `/ai/generate-image`。任意自定义地址会收到 token 和图片，不能在 Meido 中交由模型选择。 |
| default_model / nsfw_model / nsfw_enabled | nai-diffusion-4-5-curated / nai-diffusion-4-5-full / false；行为见上表。 |
| allow_txt2img / allow_img2img | 均 true；独立模式开关。 |
| auto_writeback_role_assets | false；允许时保存为角色立绘并设置聊天背景，属于额外角色资产变更。 |
| max_pixels / max_steps / default_samples | 1,048,576 / 28 / 1；default_samples 直接发给 n_samples，但当前返回路径只保存第一张。 |
| add_quality_tags / undesired_content_preset | false / 0；v4.5 请求 qualityToggle / ucPreset。 |

证据：[config.py L17–37](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/config.py#L17-L37)、[failures.py L35–70](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/failures.py#L35-L70)、[service.py L214–307](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L214-L307)。

客户端 await 单次 `POST /ai/generate-image`，JSON 为 `{input, model, action, parameters}`，action 是 generate 或 img2img；headers 带 Bearer、JSON Content-Type、`Accept: application/json, application/zip`。parameters 基本项为 width/height/steps/sampler/n_samples/negative_prompt/scale=5；v4.5 加 params_version=3、v4 正/负 caption 及固定空参考数组等；img2img 加 base64 image、strength、noise。[client.py L18–42](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/client.py#L18-L42)、[service.py L75–162](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L75-L162)、[L392–445](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L392-L445)。

源码只接受 JSON `images[0]` 为 base64 字符串；ZIP 则逐 entry 解压到内存并取第一张可识别图片，也接受原始图片。未见压缩包 entry/解压总量限制；不是解压到文件系统的 Zip Slip 路径，但存在无界内存解压风险。n_samples>1 时有付费多张、只保存一张的缺口。[service.py L460–511](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L460-L511)。

成功保存 output-1、含最终请求（包括输入图片 base64）的 request.json、meta.json 和全局 records.jsonl；公开 result 经 asdict 暴露路径，不能直接作为 Meido 模型/浏览器契约。重生成复制保存的完整请求、选不同随机 seed 并再 POST；不会重新走普通生成的模型/尺寸策略解析，不能把旧 snapshot 当当前授权。[models.py L56–104](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/models.py#L56-L104)、[service.py L164–307](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L164-L307)。

RPC 有 generate、status、history、regenerate_message_media 及 prompt_tags 的 list/upsert/delete；status 只是本地配置就绪，不能证明账号可用。history 角色过滤为空时无角色过滤。重生成以 session/message/media_index 定位旧图，进程内 set 阻止同槽并发，完成后 expected_path 比较替换；CAS 失败也可能已经付费。标签库从当前用户消息匹配、按 rating 过滤扩写提示词，是完整辅助流程，不是额外生图工具。[rpc.py L44–162](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/rpc.py#L44-L162)、[store.py L76–138](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/store.py#L76-L138)、[prompt_tags.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/prompt_tags.py)。

## 已提取：自动 CG、展示消费与危险重试

上游注册的工具 always_on=true、external_allowed=true；这不是 Meido 默认开角色授权的理由。角色 autoSceneCgEnabled 默认 false。CG 输入是宿主已提交的 SceneObservationCommitted，含 role/session/channel/chat、被动或主动来源、started/same/changed/closed/none、scene_key、visual_key、视觉描述、当前对话快照和已用工具；不是桌面截图或 P04 视觉连接。[plugin.py L134–170](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L134-L170)、[role_state.py L15–24](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/role_state.py#L15-L24)、[role_events.py L14–35](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/role_events.py#L14-L35)。

controller 取消同 session 的旧 pending，真实被动回合递增计数；无场景或 closed 不生成，当前回合已有 generate_image 则跳过。默认成功后五个回合冷却，并按 visual_key 去重；started/changed 可绕过冷却。light_provider/light_model 另做一次强制 submit_scene_image_prompt 调用，最多600 token，产出英文正负 prompt 和尺寸；之后再检查 guard、生成、message_push。辅助模型的开销/外部披露也必须授权。[auto_cg.py L23–106](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/auto_cg.py#L23-L106)、[controller L85–188](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/auto_cg_controller.py#L85-L188)、[scene_prompt.py L34–91](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/scene_prompt.py#L34-L91)。

普通工具成功图片暂存进程内 session→paths 列表；message_push 消费同路径，after_reasoning 取走剩余媒体附在回复。自动 CG 用返回文字包含“图片已发送”判断成功。该结构不是持久消费去重或真实渠道 receipt，崩溃窗口不能保证只附一次。[plugin.py L41–96](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L41-L96)、[controller L178–188](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/auto_cg_controller.py#L178-L188)。

**静态推断：同一次 CG 在相应失败条件下可产生最多六次 POST。** controller 对 Exception 重试一次；注入的 external_default HTTP 又把 POST 的 transport/timeout 和 408/429/500/502/503/504 按最多三次重试，没有限制为幂等方法。实际次数还受45秒总预算限制。即使每次 seed 相同，超时或5xx也不能证明上一次未消费。[controller L190–220](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/auto_cg_controller.py#L190-L220)、[HTTP L35–112](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/net/http.py#L35-L112)、[POST/判定 L194–217](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/net/http.py#L194-L217)、[external_default L252–256](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/net/http.py#L252-L256)。本轮没有用生成请求验证此推断。

注入链证据为 [plugin.py L116–123](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L116-L123) 的 `NovelAIClient(ctx.http, settings)`，以及宿主 [kernel.py L149–152](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/kernel.py#L149-L152) 对共享 external_default 来源的说明；不是仅根据一个未被使用的 HTTP helper 推断。

终止 controller 会 cancel 并 await 本地任务；不代表 NovelAI 端取消、未生成或退款。错误映射区分未配置、401/403、402、网络和上游错误，token 精确替换脱敏；默认模型500还会带同 token 查询硬编码 `https://api.novelai.net/user/data`。不能复制这种账号诊断副作用作为普通失败处理，更不能由失败文本直接判断未扣费。[controller L119–127](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/auto_cg_controller.py#L119-L127)、[failures.py L88–128](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/failures.py#L88-L128)、[诊断条件 service.py L520–560](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L520-L560)、[client.py L44–57](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/client.py#L44-L57)。

## 官方公开契约核验：不能从源码默认值推断当前服务

公开 Image API Swagger 确认 Bearer 生成、ZIP/JSON成功响应及另一个 SSE 接口；结构定义中的 JSON images 项是 `{image,index,seed}` 对象，与固定插件只接受字符串不同。`x-correlation-id` 用于500诊断；未发现幂等键或可恢复 job 查询/取消保证，不能据此承诺消费 exactly-once。Swagger 明写“all generation requests must be initiated by a human action”。持续开关能否覆盖无人值守 CG 尚待供应商契约确认。[官方公开 Swagger JSON，generate-image / ImageGenerationJsonResponse / ImageJson](https://image.novelai.net/docs/doc.json)、[官方 Swagger UI](https://image.novelai.net/docs/index.html)。

这些是公开文档事实，没有认证请求证据。SSE 是流式返回，不是已证实的持久 job 协议；本方案不为掉线恢复轮询虚构端点。文档未发现幂等支持，不等于证明服务永远不支持；须以厂商明确约定及受控验证补证后才可改变重试政策。

当前官方模型页已列 V5 Full/Curated，V4.5 属于较早模型；V4.5 文档也支持自然语言描述，不能把上游 ASCII tags 要求写成所有 NovelAI 模型的官方要求。移植先锁定经主人选择的具体 model ID 与请求版本，不能自动升级到“最新”或猜 V5 API ID。[官方模型说明](https://docs.novelai.net/en/image/models/)。

Anlas 与图数、设置和资格有关；多图会增加消耗。订阅页对符合分辨率/步数/单张条件的权益还有模型版本和使用限制，不能将“1MP、28步、单张”直接等同任意账号免费。seed 配合相同设置用于相似复现，并非订单号或扣费去重。[官方生图说明](https://docs.novelai.net/en/image/)、[订阅与权益](https://docs.novelai.net/en/subscription/)、[seed 说明](https://docs.novelai.net/en/image/seed/)。

官方账户设置提供 Persistent API Token，替换会使旧 token 失效；集成不需要主人的账号密码。官方 history 说明不能代替 Meido 自己的持久历史；条款还说明输入图的临时加密缓存，因此不承诺数据绝不经过或留在服务端。[官方 token 说明](https://docs.novelai.net/en/text/usersettings/account/)、[官方 FAQ](https://docs.novelai.net/en/faq/)、[服务条款](https://novelai.net/terms)。

## 已核验：Meido 可复用的边界

| 已有能力 | 可复用部分与尚缺部分 |
| --- | --- |
| SystemCredentialStore | 复用独立服务名/键的 OS 凭据存储与严格清理；NovelAI token 不进公开配置、角色模型配置、tool args、request snapshot 或日志。现有协议是 get/set，不能凭此宣称已有 NovelAI 配置/测试 UI。[credentials.py L9–32](../../backend/app/credentials.py#L9)、[读写 L108–188](../../backend/app/credentials.py#L108)。 |
| attachmentId | 现有10MiB上传上限、PNG/JPEG/WebP签名、UUID ID、角色附件目录与真实路径越界检查、无 path 的公开投影可复用；还须绑定本次原来源可访问的附件集合，不能只凭“同角色”就把其他访客图片上传外部。[chat_attachments.py L15–17](../../backend/app/chat_attachments.py#L15)、[L90–164](../../backend/app/chat_attachments.py#L90)。 |
| SessionStore / ChatView | message metadata 已能保存 attachments，UI 用角色+ID的API展示图片。可复用消息与附件表示，但持久生成 attempt、付费 dispatch、媒体消费事务、重生成附件 CAS 尚须增加；现有 update_message 不是完整生图消费协议。[SessionStore L151–180](../../backend/app/session_store.py#L151)、[L313–332](../../backend/app/session_store.py#L313)、[ChatView L21–43](../../web/src/features/chat/ChatView.tsx#L21)。 |
| TaskRuntime / completion | 可复用任务归属、预算、取消和完成回原上下文的模式；本轮核验的 aggregate HEAD 没有 submission_key/retain_until_ack 参数，不能把另一个未合入树的 seam 当当前能力。支付 attempt 状态独立持久，不能靠通用任务重启/取消推断外部交易结果。[runtime.py L189–220](../../backend/app/task_runtime/runtime.py#L189)、[completion.py](../../backend/app/task_runtime/completion.py)。 |

当前 backend 未找到 Shiori 的 SceneObservationCommitted/scene_key/visual_key 对应领域事件或 NovelAI service；不能把现有角色视觉连接当作自动 CG 已就绪。实现应增加受限的文本场景提交事件，不依赖截图或把 P04 整包引入。Skill 文字也不能承担服务鉴权、媒体归属或扣费防重的强制边界。

## 待批准：最小但完整的垂直方案

推荐独立 NovelAI 服务，第一轮完成手动与场景 CG、历史、重生成、回复展示全过程；通过不同触发许可控制真实消费。以下均为设计建议，不是已实现事实或实施批准。

1. **配置与授权。** 主人配置独立 token、固定 HTTPS 官方 origin、明确 model ID/sampler allowlist、模式、像素/步数/图数上限。生产默认服务关、每角色 manual grant 关、auto-CG grant 关、角色资产自动写回关。工具参数不能指定 endpoint、凭据、role/session/source；配置修改与撤权有代际检查。可注入的 localhost transport 只用于测试，不能成为模型自选 URL。无token/未展开env/未授权时，目录或执行返回可解释错误，绝不试生成验证。
2. **输入与结果。** 将 base_image_path 有意适配为 base_attachment_id；沿用其余参数语义并补齐 strict JSON schema、有限数值、条件必填、尺寸/模型能力校验。宿主从真实 ToolContext 捕获 role、原会话/渠道/发言者与允许的附件 ID，再解析并冻结输入内容摘要。只上传主人许可的输入图；无任意 URL、路径、整环境透传。结果只公开 generationId/attemptId、真实 seed/设置和 attachmentId，不暴露内部绝对路径、token 或完整请求体。
3. **消费预算。** 主人分别批准手动与 CG 的每请求设置、图数、每日请求/图数额度及辅助模型用量；默认没有可消费许可。图数默认1；若开放批量，必须保存、展示全部返回图并占用对应额度，不能静默丢图。计数上限只是本地风险边界，不冒充官方精确 Anlas 估价；无法确认实际扣费时保留预算占用并显示不确定。
4. **持久 attempt 与单次 dispatch。** 在任何网络发送前，SQLite 持久唯一的宿主请求键、归属、冻结的模型/参数/输入摘要、授权代际和预算预留；同调用/UI request ID/CG视觉事件重交查回原 attempt。事务将 prepared→dispatching 后才发一次 POST，禁用 HTTP 和 CG 外层自动重试。dispatch 后掉线、超时、取消、崩溃或无法验证响应，记 unknown，不自动重新发送或释放为未消费；重启也不重跑。关联ID和seed只用于追踪。无官方消费幂等保证时，这只能保证宿主不盲重发，不能保证分布式 exactly-once 或退款。
5. **任务归属与清理。** 若使用 TaskRuntime，executor 只执行该 attempt，completion 回原来源；服务并发先限制同账号1、同角色1，不引入第二个通用任务系统。通用任务取消负责停止本地资源，付费状态按 dispatch 边界判定；撤权/角色删除立即阻止排队和后续交付，已经出网的结果保留诚实状态。角色删除清理自有媒体/历史/来源索引，不删除共享账户 token；全局服务卸载再按独立设置规则清理凭据。
6. **解码与保存。** 显式选 ZIP 或官方 JSON 结构，接受经 fixture 核验的兼容格式。限制网络总字节、ZIP entry数/单项与总解压字节、图片像素/类型，拒绝非法base64/损坏ZIP/HTML伪图；不跟返回URL、不执行图片元数据。建议首轮沿用10MiB输入，输出/解压上限另设32MiB/64MiB、最多16项，均需批准并以真实尺寸 fixture 校验。保存附件及归属历史后再标成功；本地保存/投递失败只重试本地步骤，不重发生成POST。
7. **回复消费、历史与重生成。** 持久记录 generation→原消息/单次发送归属，与 SessionStore 的附件写入做幂等事务或可恢复 outbox；真实 channel receipt 与本地 message_synced 分别记录，unknown不冒充送达。history 只能查当前授权角色/来源，分页有上限，进程重建后仍可查。重生成是新的、明确计费动作：重新校验当前授权/model/输入可访问性后创建新 attempt；按旧 attachmentId/消息版本 CAS 替换指定图片，保留其他附件。CAS失败仍保存新图到历史并诚实提示，不能重新付费补偿。
8. **场景 CG。** 从已经提交的真实用户回合产生宿主身份固定的文本场景快照，保留 scene_key/visual_key、五回合冷却、started/changed 与手动已生图互斥的语义；所有触发仍受当前许可/预算/去重约束。辅助 prompt 模型独立选择稳定配置ID、限制一次调用和输出schema。排队前被新场景取代可取消；出网后不能视作未消费。保留标签库及受限扩写服务、历史浏览和重生成API/UI。无人值守 proactive CG 与官方 human-action 要求的适配仍需确认；确认前只能在明确的人类操作关联流程中验收，不能偷偷放开定时后台生成或删除整个CG范围。

需要维护者批准的具体取舍：独立服务/角色及原来源授权；base_attachment_id的兼容适配；allowed models、额度、辅助模型和资产写回许可；attempt unknown 状态与预算占用/人工重新生成语义；存储/媒体上限与保留清理；CG的合规触发契约。真实生成的账户、模型、允许的次数/成本还须另获明确授权，本轮不请求或假定已有授权。

## 待验证：不消费账号的验收与真实生成边界

| 层次 | 必须可观察的通过条件 |
| --- | --- |
| 受控 HTTP 原生 fixture | localhost 真HTTP检查Bearer只去固定origin、完整txt2img/img2img请求、精确dims/model/seed/参考图；返回ZIP及官方对象JSON，多图均保存。401/403/402/429/5xx、accept后slow/断线、损坏响应、超大/压缩炸弹时每attempt最多一POST；不把源插件错误映射当官方全部状态保证。 |
| SQLite / 崩溃恢复 | 并发同key、写attempt前/后、dispatch前/后、收响应后/保存前故障注入；prepared可安全继续，dispatching恢复unknown，重交不二次消费。未知占额度；重复本地保存/消息同步无重复附件；角色删除/撤权不跨来源交付。使用独立临时DB和模拟凭据，不连接真实vault。 |
| 工具与真实对话管线 | 脚本化provider覆盖目录/参数到HTTP再到SessionStore/UI图片；说明它只证明数据流，不证明任意模型遵从。验证无token/env缺失、错误类型/极值/额外身份字段、跨角色/跨访客底图全部在出网前拒绝；token/base64/内部path不出现在模型结果和日志。 |
| CG / 历史 / 重生成 | 真回合事件→辅助模型一次→生成一次→回复/渠道单次媒体；无scene_key、旧visual、冷却、manual互斥、新场景取消、撤权、重启、未知消耗覆盖。历史重建可查；消息有多图时只替换指定槽，CAS失败保留新结果而无第二POST。 |
| 获准真实生成 | 另行明确token配置者、具体模型/设置、最多请求数及可接受费用后，真实txt2img和owned-img2img、下载/渲染/历史/重生成/CG分别留证。不能以试用或Opus推断免费；每次重新生成也计入许可。未获此许可前只报告fixture结果，P06完整验收仍未完成。 |

本轮已做的是固定源码和公开契约核验，没有运行上述新增 fixture 或真实生图。官方实际 JSON/ZIP内容、模型ID可用性、尺寸约束、账户消耗、human-action 对自动CG的具体适用以及可能存在的厂商幂等服务仍是后续验证项；没有把缺证部分补成可用性结论。
