# 角色工作目录文件工具

状态：当前

最后核验：2026-10-15

事实来源：[工具实现](../../backend/app/agent_runtime/file_tools.py)、[行为测试](../../tests/python/test_file_tools.py)、[HTTP 验证](../../tests/python/test_file_tools_api.py)、[宿主装配](../../backend/app/main.py)。关联：#139（Parent #137）。

## 授权与调用

四个文件工具默认随角色提供，无需在 enabledTools 逐项授权。旧空列表和只保存普通资料不会移除工具；实际读写始终受角色 workspace、附件和可信来源约束，群/访客不继承主人文件权限。具体默认策略见 [ADR-0021](../adr/0021-default-builtin-tools.md)。

宿主将角色不可变 ID 和 `workspace` 绑定到工具实例；模型参数只包含路径、分页或文件内容，没有角色、根目录或身份参数。相对路径从该角色 workspace 起算；绝对路径也必须落在相同范围。跨角色、父目录逃逸、符号链接、Windows junction/reparse point、硬链接、Windows ADS 与设备文件名均拒绝。目录列表会标记链接，但不会跟随链接读取内容。

外部非主人回合不提供这些工具；工具自身还检查由宿主绑定的主人身份标记及 `ToolContext.role_id`。配对主人与桌面对话遵循相同角色授权。文件工具的范围检查不是操作系统沙箱，不提供对宿主上其他进程并发改名或挂载的隔离承诺。

| 工具 | 模型参数示例 | 行为 |
| --- | --- | --- |
| `read_file` | `{"path":"notes.txt","offset":0,"limit":50}` | 文本显示从 1 开始的行号，offset 从 0 开始；图片按文件头识别并提供临时图像内容；明显二进制拒绝作为文本读取。 |
| `list_dir` | `{"path":"."}` | 返回真实的一级目录，按名称排序，不递归或展开 glob；链接单独标记。 |
| `write_file` | `{"path":"notes/new.txt","content":"你好\n"}` | 自动创建父目录，UTF-8 整体创建或覆盖，保持参数中的换行字节，不隐式添加 BOM。返回真实 UTF-8 字节数。 |
| `edit_file` | `{"path":"notes.txt","old_text":"hello","new_text":"你好","replace_all":false}` | 原文精确替换，拒绝空匹配、未命中或未允许的多处匹配，成功返回 unified diff。不要把读取结果的行号前缀放入 old_text。 |

## 格式、结果与资源上限

文本读取去掉开头的 UTF-8 BOM；普通非 UTF-8 文本显示替代字符并明确提示。编辑只接受可严格解码的 UTF-8，保留原有 BOM。纯 CRLF 文件允许从读取结果复制的 LF 文本匹配，替换文本也统一为 CRLF；混合换行不做宽松匹配。全文覆盖精确保留 `content` 给出的 CRLF/LF。

工具返回 `AgentToolResult`，`details.status` 区分 `succeeded`、`denied`、`failed` 和 `cancelled`；后三者 `is_error=true`。成功结果包含相对来源 `path`。`truncated`、`truncatedBy` 和文字提示说明分页或资源上限。读取还返回 `totalBytes`、`totalLines`、`outputLines`、`outputBytes`、`nextOffset`；扫描未到文件末尾时 `totalLines=null`，不伪造总行数。

- 单页最多 400 行、10,000 UTF-8 字节；读取最多扫描 16 MiB，不会为巨大单行无界分配内存。超长行给出明确提示；高 offset 超过扫描预算时说明扫描上限。
- 单次创建/编辑最多 4 MiB UTF-8 字节；整体结果最多 12,000 字符，diff 单独受输出预算约束。截断 diff 不影响已经验证并完整提交的编辑内容。
- 一级目录最多扫描 5,000 项、返回 400 项，超限明确为部分结果；扫描上限内排序，未声称是整个目录的完整排序。
- 图片输入最多 16 MiB、4,000 万像素；直接编码预算为 800 万 base64 字符。需要压缩时转 JPEG、最长边 1,568 像素；超限或损坏图片明确失败。宿主可以指定 `multimodal=False`，此时返回需要视觉模型的说明，不伪造图片理解。当前模型连接没有视觉能力配置字段，默认会把图片交给已支持多模态内容的适配层；实际模型须支持图片。

图片只经 `AgentToolResult.images` 进入本次运行后续模型请求；不把 base64 写入工具文本、SSE 或持久化对话历史。工作线程执行有界 I/O，并检查取消信号。相同文件的读写跨工具实例序列化；写入采用同目录临时文件和原子替换，失败或取消清理临时文件，不保留半写入的目标内容。已完成原子提交后的取消不能撤销先前成功的文件效果。

## 上游来源与许可

行为基线是 [Shiori-Agent `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `filesystem.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py)。复刻其四工具接口、分页行号、图像结果、UTF-8 BOM/CRLF 兼容、唯一匹配拒绝与 unified diff 行为。

Meido 使用自己的 `AgentTool`、`ToolContext`、角色授权、结构化结果、取消和持久化协议；路径隔离、有界扫描、写入原子性和锁在本项目实现，不引入 `shiori_sdk` 或上游运行时。为保留来源与改编行为的许可声明，随附 [Shiori-Agent MIT License](../../third_party/shiori-agent/LICENSE)。
