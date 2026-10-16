"""Twelve A01 tools; ordinary chat and Drift use different dispatchers."""
def _schema(name, description, properties=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required), "additionalProperties": False}}}


_IDS = {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 128}}
_REASON = {"type": "string", "maxLength": 1000}
SCHEMAS = (
    _schema("get_alert_events", "读取本tick冻结的告警事件，不重新访问来源。"),
    _schema("get_content_events", "读取本tick冻结的内容候选及不透明item ID。"),
    _schema("get_context_data", "读取已获准的冻结上下文，不重新poll。"),
    _schema("recall_memory", "只检索当前角色及冻结目标获准记忆，最多2项；未命中不表示不感兴趣。",
            {"query": {"type": "string", "minLength": 1, "maxLength": 2000}}, ("query",)),
    _schema("get_content", "批量读取当前候选的缓存正文；正文失败不代表不感兴趣。",
            {"item_ids": _IDS}, ("item_ids",)),
    _schema("web_fetch", "在已有网页许可内核实来源，复用现有URL安全校验。",
            {"url": {"type": "string", "minLength": 1, "maxLength": 8192}}, ("url",)),
    _schema("web_search", "在已有网页许可内搜索补充资料。",
            {"query": {"type": "string", "minLength": 1, "maxLength": 2000},
             "type": {"type": "string", "maxLength": 100}}, ("query",)),
    _schema("get_recent_chat", "读取冻结目标最近普通聊天片段，排除自主及内部任务消息。",
            {"n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20}}),
    _schema("message_push", "只暂存一次非空草稿；随后finish_turn(reply)才提交。evidence仅当前alert/content ID。",
            {"message": {"type": "string", "minLength": 1, "maxLength": 4000},
             "mood": {"type": "string", "maxLength": 100},
             "thought": {"type": "string", "maxLength": 1000},
             "evidence": {**_IDS, "minItems": 0},
             "account_id": {"type": "string", "maxLength": 256},
             "target_id": {"type": "string", "maxLength": 256},
             "account_channel": {"type": "string", "maxLength": 64},
             "target_kind": {"type": "string", "maxLength": 64}}, ("message", "mood", "thought")),
    _schema("mark_interesting", "明确标记已评估的当前内容；不写长期兴趣。",
            {"item_ids": _IDS, "reason": _REASON}, ("item_ids",)),
    _schema("mark_not_interesting", "明确丢弃本质无价值的当前内容；不能自动从抓取失败推导。",
            {"item_ids": _IDS, "reason": _REASON}, ("item_ids",)),
    _schema("finish_turn", "终结一次：reply要求已有草稿；skip要求无草稿及合法reason。缺finish不会自动发送。",
            {"decision": {"type": "string", "enum": ["reply", "skip"]},
             "reason": {"type": "string", "enum": ["no_content", "user_busy", "already_sent_similar", "scene_changed", "other"]},
             "note": {"type": "string", "maxLength": 1000}}, ("decision",)),
)
