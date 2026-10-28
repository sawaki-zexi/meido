"""Story-owned image copies and frozen role visuals, independent of role lifetime.

Resource adoption adapted from Shiori-Agent d3acaed (MIT; SHIORI-LICENSE.txt).
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def copy_image(source: Path, directory: Path) -> str:
    """Copy validated raster media under a content-addressed, Story-owned name."""
    from ..novelai_generation.media import validate_image
    content = source.read_bytes()
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("故事图片超过 20 MiB")
    validate_image(content, max_pixels=20 * 1024 * 1024)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / hashlib.sha256(content).hexdigest()
    if not destination.exists():
        temporary = destination.with_suffix('.tmp')
        temporary.write_bytes(content)
        temporary.replace(destination)
    return str(destination)


def adopt_image(database_path: Path, value: str, workspace: Path | None = None) -> str:
    source = Path(value)
    if not source.is_absolute():
        source = (workspace or database_path.parent) / source
    if source.resolve().is_relative_to((database_path.parent / "assets").resolve()):
        return str(source)
    if not source.is_file():
        raise ValueError("生成图片文件不存在")
    return copy_image(source, database_path.parent / "assets")


def role_snapshot(role, database_path: Path, roles=None):
    """Freeze role semantics without model secrets or tool grants."""
    profile = role.profile
    result = {"id": role.id, "name": role.name, "description": role.description,
              "background": profile.profile,
              "system_prompt": '\n'.join([profile.profile, profile.personality, profile.behaviorRules, profile.responseConstraints]),
              "avatar": role.avatarUrl, "illustrations": [], "chatBackgroundUrl": None}
    if roles is None:
        return result
    directory = database_path.parent / 'assets'
    def freeze(path):
        if not path or not Path(path).is_file():
            return None
        return copy_image(Path(path), directory)
    result['avatar'] = freeze(roles.avatar_path(role.id))
    default_mood = getattr(role, 'defaultMood', '平静')
    bindings = getattr(role, 'moodIllustrationBindings', {})
    result['defaultMood'] = default_mood
    result['moodIllustrationBindings'] = {}
    frozen_illustrations = {}
    background_id = getattr(role, 'chatBackgroundId', None)
    resolver = getattr(roles, 'illustration_path', None)
    if resolver:
        for item in getattr(role, 'illustrations', []):
            identifier = item.id if hasattr(item, 'id') else item['id']
            frozen = freeze(resolver(role.id, identifier))
            if frozen:
                frozen_illustrations[identifier] = frozen
                if identifier == background_id:
                    result['chatBackgroundUrl'] = frozen
        result['moodIllustrationBindings'] = {
            mood: frozen_illustrations[identifier] for mood, identifier in bindings.items()
            if identifier in frozen_illustrations}
        default = result['moodIllustrationBindings'].get(default_mood)
        if default:
            result['illustrations'].append(default)
        result['illustrations'].extend(path for identifier, path in frozen_illustrations.items()
                                      if identifier != background_id and path not in result['illustrations'])
    # The selected scene background is not a character foreground. Respect the
    # default mood binding, then the character library; legacy card is fallback.
    if not result['illustrations']:
        card = freeze(roles.root / role.id / 'card-image')
        if card:
            result['illustrations'].append(card)
    return result
