# 固定本机数据目录启动与备份

状态：当前

事实来源：[`Start-Meido.ps1`](../../scripts/local-runtime/Start-Meido.ps1)、[`backup-meido-data.py`](../../scripts/local-runtime/backup-meido-data.py)、[`main.py`](../../backend/app/main.py)、[`storage.py`](../../backend/app/storage.py)

最后核验：2026-10-16

## 保留一份本机路径配置

默认启动会相对于工作目录寻找 `.data` 和 `roles`。换到另一个 Git 工作区启动时，可能读到另一份空目录，看起来像模型或角色丢失。已保存的模型、向量配置和聊天等数据仍在原目录；先找回原目录，再固定启动路径。

将 [`settings.example.json`](../../scripts/local-runtime/settings.example.json) 复制到工作区外的固定位置，例如 `D:\MeidoRuntime\local-settings.json`，编辑其中的绝对路径：

| 字段 | 用途 |
| --- | --- |
| `codeRoot` | 要运行的代码工作区，需安装依赖 |
| `projectRoot` | 稳定的项目级资源根目录，例如项目级技能 |
| `dataDir` | 模型/向量配置、记忆开关、聊天/渠道、记忆、待办、邮件等文件与数据库 |
| `rolesDir` | 角色、头像、卡图、附件、记忆文档和角色技能 |
| `ownerKnowledgeDir` | 主人资料镜像及索引 |
| `browserUseDataDir` | 浏览器登录状态等数据 |
| `python` / `node` | 已安装的 Python 和 Node 可执行文件绝对路径 |
| `apiHost` / `apiPort` / `webPort` | 本地后端监听地址、后端及前端端口 |
| `backupRoot` | 独立的快照保存目录，应位于上述数据目录之外 |
| `legacyModelConfigs` | 需要单独归档的旧模型配置文件绝对路径，可留空 |

示例中的路径需要改成自己机器上的实际位置。已有数据直接指向旧目录即可；首次安装可先创建固定的空数据、角色和主人资料目录。脚本遇到这些目录缺失时会报错，避免误用新目录。

脚本默认读取自身目录下的 `local-settings.json`，该文件已被 Git 忽略。长期使用时，推荐把配置放在工作区外，并每次传入同一个 `-SettingsPath`。配置文件只保存路径和启动选项，模型密钥继续由应用保存到数据目录。

## 检查并启动

启动脚本面向 Windows PowerShell；备份脚本仅依赖 Python 标准库。

在代码工作区运行以下命令，先确认路径；检查模式输出 JSON，不启动服务、不创建数据：

```powershell
& .\scripts\local-runtime\Start-Meido.ps1 -SettingsPath D:\MeidoRuntime\local-settings.json -CheckOnly
```

在两个 PowerShell 终端分别启动后端和前端：

```powershell
& .\scripts\local-runtime\Start-Meido.ps1 -SettingsPath D:\MeidoRuntime\local-settings.json -Service Backend
```

```powershell
& .\scripts\local-runtime\Start-Meido.ps1 -SettingsPath D:\MeidoRuntime\local-settings.json -Service Frontend
```

按示例端口访问 <http://127.0.0.1:5288/app>。脚本通过现有 `MEIDO_*` 环境变量设置路径，后端端口传给 uvicorn，前端代理指向相同后端。已有服务在目标端口响应时，脚本保留它；端口占用但服务不响应时会报错。终端关闭会结束其前台服务，已保存数据仍在磁盘上。

更换代码版本前在原终端停止旧服务，再修改配置的 `codeRoot`，或临时指定 `-CodeRoot D:\另一个代码工作区`。同一份数据只运行一个后端，以免渠道重复连接或多个进程同时修改配置。只有 `codeRoot` 随代码版本变化，数据目录保持固定。

## 创建备份

```powershell
& .\scripts\local-runtime\Start-Meido.ps1 -SettingsPath D:\MeidoRuntime\local-settings.json -Service Backup
```

也可直接调用备份脚本：

```powershell
python .\scripts\local-runtime\backup-meido-data.py --settings D:\MeidoRuntime\local-settings.json
```

每次生成独立快照，保留数据、角色、主人资料、本机路径配置和旧模型配置。位于数据目录外的浏览器数据也会单独归档。SQLite 使用在线备份并执行完整性检查；数据根目录下的 `pytest*` 测试目录不进入快照。旧模型文件只放进 `legacy`，不合并或覆盖当前配置。

完成后检查 `manifest.json` 的 `status` 为 `complete`。失败会返回非零状态并保留 `incomplete` 标记，不能把失败快照当成可恢复备份。在线备份保证每个数据库的一致性；若需要所有数据库、角色文件处于统一时间点，先停止后端和浏览器并暂停编辑，再运行备份。

快照含本地模型密钥与个人数据，应保存在可信本机存储，不提交 Git。飞书、邮件、渠道等使用 Windows 凭据库的授权不在目录备份内；换电脑或 Windows 用户时通常需要重新授权。

## 恢复已保存数据

1. 停止后端和相关浏览器，先备份当前数据。
2. 确认快照 `manifest.json` 标为 `complete`，按 `sources` 中的源/目标映射找回数据目录。
3. 将所需文件或目录恢复到固定数据路径；不要在服务运行时覆盖 SQLite 数据库。
4. 按需要恢复 `runtime/local-settings.json` 并核对本机路径，重新启动后在设置中检查模型、角色及其他配置。

保留旧模型而继续使用当前模型时，只需归档 `legacy`；需要启用旧配置时先核对其来源和密钥，避免直接覆盖新配置。未点击保存的表单草稿不属于磁盘配置，目录备份无法恢复这类内容。
