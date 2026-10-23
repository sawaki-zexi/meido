# 角色设定与清单版本

状态：当前
事实来源：[模型](../../backend/app/models.py)、[迁移链](../../backend/app/role_migration.py)、[角色存储](../../backend/app/role_store.py)、[迁移测试](../../tests/python/test_role_manifest_migration.py)、[API 测试](../../tests/python/test_api.py)
最后核验：2026-10-23

## 保存与 API 契约

角色清单为 JSON，根节点版本为 2。角色资料的规范 profile 格式为：

    {
      "version": 1,
      "character": {
        "profile": "身份与背景",
        "personality": "性格",
        "behaviorRules": "行为规则",
        "responseConstraints": "回复约束",
        "nickname": "主人"
      },
      "importProvenance": {
        "format": "json",
        "source": "character.json",
        "importedAt": "2026-10-23T00:00:00Z"
      }
    }

名称、简介仍属于 Role；模型绑定、能力、头像与卡图字段不移入 profile。新建、更新、列表和详情响应统一返回嵌套结构。角色设定不能为空，其他四个字符字段允许空字符串。profile 版本只接受整数 1，不接受未知版本或布尔值。

创建和编辑 API 仍接受旧的五字段扁平 profile。编辑对已提交的 profile 字段做递归合并，省略来源或其他字段不会清除已有值；明确提交空字符串可清除可选角色字段，明确提交 importProvenance 为 null 可移除来源。Python 的旧五字段只读属性保留兼容；生产 prompt 读取 character。

## 迁移与恢复

RoleStore 在发布内存角色前校验整份清单，按迁移链将 v1 升级到 v2。v1 的旧五字段移入 character，未识别的原始字段保留；旧记录缺少 ID/时间戳时一次性生成并随迁移落盘。已有 ID、同名角色、时间戳、素材 URL、模型绑定和能力配置保留。重复启动不会重写已升级文件。

校验成功后，使用同目录的唯一临时文件和原子替换提交；失败保留原文件并清理临时文件。损坏 JSON、非法 UTF-8、错误根节点、错误角色数组、未来版本、重复 ID 或损坏的新版角色会拒绝加载。角色字段错误只报告清单版本和角色索引，避免把原始私有输入放入异常信息。此机制不提供跨进程写锁，也不自动修复未知 schema。

未知存储字段保留在原始清单中，不进入 API/runtime 模型；普通保存与同进程删除回滚也会保留这些字段。它们并不因此获得任何运行权限。

## 导入来源

JSON、PNG、APNG、CHARX 预览返回 importProvenance 候选：文件格式、去除目录的文件名和 UTC 预览时间。只有主人接受预览并保存角色后才持久化；取消预览不改变资料。格式不是角色卡 schema 版本。来源不保存本机完整路径、文件正文或模型凭据，也不参与提示词。

浏览器草稿继续使用表单字段保存到当前标签页 sessionStorage；来源作为独立字段恢复。旧草稿没有来源时按 null 读取。资料保存不提交工具配置；角色卡填充仍保持既有能力配置。

提示词独立编译器由 #207 管理；模型配置规范化由 #205 管理，导出由 #206 管理。本契约不声称这些后续工作已经完成。
