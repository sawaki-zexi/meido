import asyncio
import io

from PIL import Image

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.models import RoleInput
from backend.app.role_mood import RoleMoodTool
from backend.app.role_store import RoleStore


def test_role_mood_uses_owned_binding_and_frozen_live_desktop_run(tmp_path):
    store = RoleStore(tmp_path / 'roles')
    role = store.create(RoleInput(name='诗织', profile={'profile': 'test'}))
    output = io.BytesIO()
    Image.new('RGB', (32, 32), 'blue').save(output, format='PNG')
    role = store.add_illustration(role.id, output.getvalue(), 'image/png')
    identifier = role.illustrations[0].id
    role = store.update_illustrations(role.id, mood_bindings={'开心': identifier}, default_mood='开心')
    authorized = True
    tool = RoleMoodTool(store, role, authorized=lambda context: authorized)
    tool.bind_run('run-1', f'role:{role.id}')
    context = ToolContext(role.id, f'role:{role.id}', 'run-1', CancellationToken())
    result = asyncio.run(tool.execute({'mood': '开心'}, context))
    assert not result.is_error and result.details['illustrationId'] == identifier
    persisted = RoleStore(store.root).get(role.id)
    assert persisted.currentMood == '开心' and persisted.currentMoodUpdatedAt
    assert not persisted.illustrationCategorySend.get('未分类', False)
    assert asyncio.run(tool.execute({'mood': '不存在'}, context)).is_error
    assert asyncio.run(tool.execute({'mood': '开心'}, ToolContext(role.id, f'role:{role.id}', 'other-run', CancellationToken()))).is_error
    authorized = False
    assert asyncio.run(tool.execute({'mood': '开心'}, context)).is_error
    authorized = True
    store.update_illustrations(role.id, mood_bindings={})
    assert asyncio.run(tool.execute({'mood': '开心'}, context)).is_error
    assert store.get(role.id).currentMood is None
