"""Bounded Unicode scalar text, shared by generation and saved prompt drafts."""
import re


class PromptValidationError(ValueError):
    pass


def validate_prompt(value: str, *, ascii_only=False, required=False) -> str:
    # Python counts codepoints. Check the submitted length before trimming only
    # ASCII edge spaces, keeping combining marks and all other Unicode intact.
    if not isinstance(value, str) or len(value) > 4000:
        raise PromptValidationError('提示词最多 4000 字符，组合字符分开计数。')
    if re.search(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029]', value):
        raise PromptValidationError('提示词不能包含换行、控制字符或无效 Unicode。')
    if required and (not value or value.isspace()):
        raise PromptValidationError('请填写正向提示词。')
    if ascii_only and not value.isascii():
        raise PromptValidationError('V4.5 提示词只能使用英文 ASCII，请选择 V5 或调整输入。')
    return value.strip(' ')
