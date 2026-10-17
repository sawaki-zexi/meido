"""The supported builtin mode profile is host provenance, never model input."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

BUILTIN_ROOT = Path(__file__).resolve().parents[1] / 'builtin_skills'
SKILL_SHA = '972dc06af0cd269d6d9b2900448c2bd900be6d75b1c0f30c181e1c8bb35976c9'
PROFILE_SHA = '62143d5248aa431f78b28a6489ef409e8ca40667eab557a866b8e69ddd614c1a'
PROFILE_PATH = 'meme-manage/references/drift-meme.json'
TOOLS = ('read_file', 'list_dir', 'write_file', 'edit_file', 'meme_copy_image', 'finish_drift')


def is_profile(skill):
    return (skill is not None and skill.source == 'builtin' and skill.skill_id == 'meme-manage' and skill.version == '1.1.0'
        and skill.content_hash == SKILL_SHA and skill.tools == TOOLS)


def effective(skill, root):
    if skill.source != 'builtin' or skill.skill_id != 'meme-manage':
        return skill
    resources = skill.supporting_files
    if (Path(root).absolute() != BUILTIN_ROOT.absolute() or skill.version != '1.1.0'
            or skill.content_hash != SKILL_SHA or len(resources) != 1
            or resources[0].path != PROFILE_PATH or resources[0].content_hash != PROFILE_SHA):
        raise ValueError('unsupported builtin meme instruction bundle')
    raw = (BUILTIN_ROOT / PROFILE_PATH).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROFILE_SHA:
        raise ValueError('builtin meme mode profile changed')
    profile = json.loads(raw)
    if profile['profile'] != 'meido-drift-meme-v1' or tuple(profile['tools']) != TOOLS:
        raise ValueError('builtin meme mode declaration changed')
    return replace(skill, tools=TOOLS, body=profile['instructions'], body_path=None, root_path=None)
