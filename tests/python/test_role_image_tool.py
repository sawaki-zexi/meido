"""Role images are inspectable without broadening the role workspace grant."""
import asyncio
import base64
import io
import os
from dataclasses import replace

from PIL import Image

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.models import RoleInput
from backend.app.role_store import RoleStore


def image_bytes(color):
    output = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(output, format='PNG')
    return output.getvalue()


def setup_role(tmp_path):
    from backend.app.agent_runtime.role_image_tool import InspectRoleImageTool
    store = RoleStore(tmp_path / 'roles')
    role = store.create(RoleInput(name='Alice', profile={'profile': '银发蓝眼'}))
    raw = image_bytes('blue')
    role = store.set_avatar(role.id, raw, 'image/png')
    role = store.add_illustration(role.id, raw, 'image/png', '日常立绘')
    context = ToolContext(role.id, f'role:{role.id}', 'run', CancellationToken())
    return store, role, raw, context, InspectRoleImageTool


def test_role_image_catalog_and_pixels_are_scoped_to_role_not_workspace(tmp_path):
    store, role, raw, context, Tool = setup_role(tmp_path)
    tool = Tool(store, role, authorized=lambda context: True)
    catalog = asyncio.run(tool.execute({}, context))
    assert [item['asset_id'] for item in catalog.details['images']] == ['avatar', role.illustrations[0].id]
    assert not catalog.images
    for asset_id in ('avatar', role.illustrations[0].id):
        result = asyncio.run(tool.execute({'asset_id': asset_id}, context))
        assert not result.is_error
        assert base64.b64decode(result.images[0].data_base64) == raw
        assert str(store.root) not in result.content
    assert not list(store.workspace_path(role.id).iterdir())


def test_image_tool_rejects_foreign_paths_changed_images_and_revoked_source(tmp_path):
    store, role, raw, context, Tool = setup_role(tmp_path)
    authorized = True
    tool = Tool(store, role, authorized=lambda context: authorized)
    for invalid in ('../roles.json', 'avatar-original', 'https://example.com/image.png'):
        assert asyncio.run(tool.execute({'asset_id': invalid}, context)).is_error
    assert asyncio.run(tool.execute({}, replace(context, role_id='another-role'))).is_error
    other = store.create(RoleInput(name='Bob', profile={'profile': 'private'}))
    other = store.add_illustration(other.id, image_bytes('red'), 'image/png')
    assert asyncio.run(tool.execute({'asset_id': other.illustrations[0].id}, context)).is_error
    store.set_avatar(role.id, image_bytes('green'), 'image/png')
    assert asyncio.run(tool.execute({'asset_id': 'avatar'}, context)).is_error
    authorized = False
    assert asyncio.run(tool.execute({}, context)).is_error


def test_image_tool_blocks_links_and_text_only_models(tmp_path):
    store, role, raw, context, Tool = setup_role(tmp_path)
    result = asyncio.run(Tool(store, role, authorized=lambda context: True, multimodal=False)
        .execute({'asset_id': 'avatar'}, context))
    assert result.is_error and not result.images
    avatar = store.avatar_path(role.id)
    avatar.unlink()
    target = store.illustration_path(role.id, role.illustrations[0].id)
    if os.name == 'nt':
        os.link(target, avatar)
    else:
        avatar.symlink_to(target)
    result = asyncio.run(Tool(store, role, authorized=lambda context: True)
        .execute({'asset_id': 'avatar'}, context))
    assert result.is_error and not result.images
