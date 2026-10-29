from datetime import datetime, timezone

import pytest

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


def test_only_host_marked_actual_desktop_human_run_can_claim_generation(tmp_path):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.novelai_generation.context import capture_tool_context
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('role-171')
    sessions.create_run_with_messages(AgentRun(runId='run-171', roleId='role-171', sessionKey='role:role-171',
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={}), 'create an image')
    context = ToolContext('role-171', 'role:role-171', 'run-171', CancellationToken(), 'call-171')
    with pytest.raises(PermissionError):
        capture_tool_context(sessions, None, context, role_id='role-171', is_owner=True)
    sessions.update_run('run-171', status='running', model_snapshot={'humanInputSource': 'desktop_owner'})
    source = capture_tool_context(sessions, None, context, role_id='role-171', is_owner=True)
    assert source.human_key == 'run-171'
    assert source.origin.session_key == 'role:role-171'
    assert source.attachment_ids == frozenset()
    with pytest.raises(PermissionError):
        capture_tool_context(sessions, None, context, role_id='other-role', is_owner=True)


def test_guest_channel_generation_keeps_its_real_identity_and_revocable_source(tmp_path):
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.models import AgentRun
    from backend.app.multichannel import MultichannelStore
    from backend.app.session_store import SessionStore
    from backend.app.novelai_generation.context import capture_tool_context
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role', 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('role', 'qq', account.account_id, '100', 'group')
    _, message = channels.append_event(thread, direction='inbound', content='画一片天空', external_message_id='1',
        metadata={'senderId': '22', 'senderIsOwner': False})
    sessions.create_run_with_existing_user_message(AgentRun(runId='guest', roleId='role', sessionKey=thread.session_key,
        status='running', startedAt=datetime.now(timezone.utc)), message.id)
    context = ToolContext('role', thread.session_key, 'guest', CancellationToken(), 'image')
    source = capture_tool_context(sessions, channels, context, role_id='role', is_owner=False,
        source_thread_id=thread.thread_id, source_account_id=account.account_id)
    assert source.channel_source.sender_id == '22'
    assert source.channel_source.owner_context is False
    assert source.denial(channels) is None
    with pytest.raises(PermissionError):
        capture_tool_context(sessions, channels, context, role_id='role', is_owner=True)
    channels.set_account_state(account.account_id, enabled=False)
    assert source.denial(channels) is not None


@pytest.mark.parametrize('allowed', [True, False])
def test_img2img_tool_uses_only_image_ids_in_this_real_human_message(novelai_service, allowed):
    import asyncio
    import io
    import zipfile
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    from backend.app.chat_attachments import save_attachment, public_attachment
    service = novelai_service
    with zipfile.ZipFile(io.BytesIO(service.peer.body)) as archive:
        png = archive.read('image.png')
    item = save_attachment(service.roles.workspace_path(service.role.id), 'current.png', 'image/png', png)
    key = f'role:{service.role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='image-run', roleId=service.role.id, sessionKey=key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'}),
        'use the image', attachments=[public_attachment(item)] if allowed else [])
    tool = service.application.tool_for(service.role.id, is_owner=True)
    result = asyncio.run(tool.execute({**service.request, 'mode': 'img2img', 'base_attachment_id': item['id']},
        ToolContext(service.role.id, key, 'image-run', CancellationToken(), 'image-call')))
    assert result.is_error is not allowed
    assert len(service.peer.requests) == (1 if allowed else 0)
