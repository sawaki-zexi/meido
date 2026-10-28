"""Local readiness only: never read credentials or probe a paid provider."""
from __future__ import annotations


def state(code: str, message: str) -> dict[str, object]:
    return {'available': code == 'ready', 'code': code, 'message': message}


def configuration_availability(config) -> dict[str, object]:
    if not config['enabled']:
        return state('service_disabled', 'NovelAI 生图已关闭，请在插件设置中启用。')
    if config['credentialCleanupPending']:
        return state('credential_cleanup_pending', 'NovelAI 凭据操作未完成，请在设置中重新保存或清空凭据。')
    if not config['tokenConfigured']:
        return state('credential_missing', '尚未配置 NovelAI 凭据，请在插件设置中保存 Token。')
    if not config['allowTxt2Img'] and not config['allowImg2Img']:
        return state('generation_modes_disabled', '文生图和图生图均已关闭，请在 NovelAI 设置中开启所需模式。')
    return state('ready', '本地配置已就绪，可发起生图；Token 有效性和账户额度将在实际生成时验证。')


class GenerationUnavailable(PermissionError):
    """A deliberately public, credential-free admission explanation."""
    def __init__(self, availability):
        super().__init__(availability['message'])
        self.code = availability['code']
