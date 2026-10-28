"""Safe public explanations from stable error codes, never provider response text."""
from __future__ import annotations


def generation_error_message(code: str) -> str:
    if not code:
        return ''
    messages = {
        'http_401': 'NovelAI Token 无效或已过期，请在插件设置中重新保存凭据。',
        'http_403': 'NovelAI 拒绝访问，请检查 Token 与账户的生图权限。',
        'http_402': 'NovelAI 账户的 Anlas 余额或生图额度不足，请检查账户。',
        'http_400': 'NovelAI 拒绝了生成参数，请检查模型、尺寸和提示词。',
        'http_422': 'NovelAI 无法接受当前生成参数，请检查模型、尺寸和提示词。',
        'http_429': 'NovelAI 请求过于频繁。本次结果无法确认，请查看原记录；不会自动重发。',
        'transport_unknown': '网络连接中断或超时，无法确认本次结果；可能已计费，原请求不会再次发送。',
        'invalid_response_type': 'NovelAI 返回的内容不是可识别的图片或图片包，请检查服务连接；原请求不会再次发送。',
        'unsupported_content_encoding': 'NovelAI 返回了不支持的压缩格式，未能读取图片；原请求不会再次发送。',
        'invalid_content_encoding': 'NovelAI 返回的压缩数据不完整或已损坏，未能读取图片；原请求不会再次发送。',
        'response_budget_exceeded': 'NovelAI 返回的图片数据超过本地大小限制，未能保存完整图片；原请求不会再次发送。',
        'empty_response': 'NovelAI 返回了空响应，未收到图片；原请求不会再次发送。',
        'local_media_pending': '图片本地保存尚未完成，只恢复本地步骤，不会重新请求生图。',
        'response_not_verifiable': '无法完整验证远端图片，请查看本次记录；不会重新请求生图。',
        'unexpected_multiple_images': '返回图片张数与请求不一致，已保留收到的图片，请在历史查看。',
        'replacement_conflict': '已生成，原消息已改变；新图仅保留在历史，未替换其他附件。',
        'scene_none': '当前没有需要生成图片的场景。',
        'scene_closed': '场景已结束，本次不生成图片。',
        'scene_cooldown': '当前场景仍在五个主人回合的冷却期内。',
        'scene_visual_duplicate': '此画面已经处理，本次不重复生成。',
        'scene_observation_failed': '场景观察未完成；辅助请求可能已经计费，不会自动重试。',
    }
    if code in messages:
        return messages[code]
    if code.startswith('http_5'):
        return 'NovelAI 服务暂不可用。本次结果无法确认，可能已计费；请查看原记录，不会自动重发。'
    if code.startswith('http_') and code[5:].isdigit():
        return f'NovelAI 返回 HTTP {code[5:]}，本次请求未正常完成；原请求不会再次发送。'
    return '本次生图未正常完成，请检查设置和原记录；可能已出网的请求不会自动重发。'


class GenerationBudgetExceeded(ValueError):
    code = 'daily_limit'
    message = '今日生图尝试次数已达上限，请明日再试或在设置中调整次数上限。'

    def __init__(self):
        super().__init__(self.message)
