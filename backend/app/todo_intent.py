from __future__ import annotations

import re


_ACTION_PATTERNS = (
    ("complete", r"(?:标记为已完成|已经完成|完成了|做完了|搞定了|交了|提交了|完成|做完|搞定)"),
    ("cancel", r"(?:取消|不用做|不做了|删掉|删除)"),
    ("restore", r"(?:恢复|重新安排|重新打开)"),
    ("update", r"(?:延期|推迟|提前|改期|改到|改成|修改|更新)"),
)


def explicit_todo_operation(content: str) -> str | None:
    text = content.strip()
    if re.search(r"(?:可能|也许|或许|说不定|考虑|假如)", text):
        return None
    for operation, pattern in _ACTION_PATTERNS:
        if re.search(r"(?:请|帮我|把|将).{0,60}" + pattern, text) or re.search(r"我(?:已经|刚刚|已)?" + pattern + r".{0,40}", text):
            if re.search(r"(?:不是|并非|不要|不必|不用|不需要).{0,12}" + pattern, text):
                continue
            return operation
    creation_cue = next((value for value in ("提醒我", "记一下", "记下", "别忘了", "不要忘记", "千万别忘") if value in text), None)
    if creation_cue is not None:
        if re.search(r"(?:不用|不需要|不必|不想|不要)\s*$", text.split(creation_cue, 1)[0]):
            return None
        return "create"
    if "待办" in text and re.search(r"(?:加入|加到|添加|创建|新增|安排|设置).{0,12}待办|待办(?:是|：|:)|把.{1,40}设为待办", text):
        return "create"
    return None


def explicit_todo_cue(content: str) -> str | None:
    text = content.strip()
    operation = explicit_todo_operation(text)
    if operation in {"complete", "cancel", "restore", "update"}:
        return "操作待办"
    if operation is None:
        return None
    cue = next((value for value in ("提醒我", "记一下", "记下", "别忘了", "不要忘记", "千万别忘") if value in text), "待办")
    if cue in {"记一下", "记下"} and re.search(r"(?:我喜欢|我不喜欢|偏好|我的名字|我叫)", text.split(cue, 1)[1]):
        return None
    return cue


def is_explicit_todo_request(content: str) -> bool:
    return explicit_todo_cue(content) is not None
