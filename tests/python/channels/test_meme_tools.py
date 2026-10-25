import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.meme_tools import ChatMemeTools


@dataclass
class Candidate:
    id: str = 'candidate'

    def public(self):
        return {'id': self.id, 'category': 'happy', 'description': '开心', 'aliases': []}


class Library:
    def __init__(self):
        self.saved = []
        self.frozen = []
        self.current = True

    def freeze_attachment(self, role, item):
        self.frozen.append(item)
        return item

    def save(self, role, item, category, description, context, authorized):
        assert authorized()
        self.saved.append(item)
        return {'status': 'recorded', 'deduplicated': False}

    def candidates(self, role, query='', context=None):
        return (Candidate(),)

    def valid(self, role, candidate):
        return self.current

    def reference(self, role, candidate, context):
        return {'id': 'attachment'}, b'real fixture covered by library integration'


class Host:
    def __init__(self):
        self.sent = []
        self.status = 'sent'
        self.before_send = lambda: None

    async def account_send(self, *args, **kwargs):
        self.before_send()
        assert kwargs['authorize_source']()
        self.sent.append((args, kwargs))
        return SimpleNamespace(status=SimpleNamespace(value=self.status), attempt_id='attempt', platform_message_id='message', error=None)


def setup(*, attachments=None, permissions=None, authorized=lambda: True):
    context = ToolContext('role', 'thread:source', 'run', CancellationToken(), 'call')
    library, host = Library(), Host()
    service = ChatMemeTools(library, host, role_id='role', session_key='thread:source', run_id='run',
        account_id='account', thread_id='source', target_id='target',
        attachments=attachments or [], authorized=authorized,
        permitted=lambda name: name in (permissions if permissions is not None else {'read_file', 'write_file', 'account_send'}))
    return context, library, host, {tool.definition.name: tool for tool in service.tools()}


def call(tools, name, arguments, context):
    return asyncio.run(tools['chat_meme_' + name].execute(arguments, context))


def test_save_requires_unique_current_host_attachment():
    context, library, _, tools = setup(attachments=[{'id': 'a', 'kind': 'image'}, {'id': 'b', 'kind': 'image'}])
    assert call(tools, 'save', {'category': 'happy', 'description': '开心'}, context).is_error
    assert call(tools, 'save', {'attachment_id': '/other/role/image.png', 'category': 'happy', 'description': '开心'}, context).is_error
    assert not library.saved
    assert not call(tools, 'save', {'attachment_id': 'b', 'category': 'happy', 'description': '开心'}, context).is_error
    assert library.saved == [{'id': 'b', 'kind': 'image'}]


@pytest.mark.parametrize('permissions', [set(), {'read_file'}, {'write_file'}, {'account_send'}])
def test_save_never_borrows_missing_authority(permissions):
    context, library, _, tools = setup(attachments=[{'id': 'a', 'kind': 'image'}], permissions=permissions)
    assert call(tools, 'save', {'category': 'happy', 'description': '开心'}, context).is_error
    assert not library.saved


def test_send_requires_listed_candidate_rechecks_material_and_reuses_uncertain_result():
    context, library, host, tools = setup()
    assert call(tools, 'send', {'candidate_id': 'candidate'}, context).is_error
    assert not call(tools, 'list', {}, context).is_error
    host.status = 'uncertain'
    first = call(tools, 'send', {'candidate_id': 'candidate'}, context)
    assert json.loads(first.content)['status'] == 'uncertain'
    assert call(tools, 'send', {'candidate_id': 'candidate'}, context).content == first.content
    assert len(host.sent) == 1
    args, kwargs = host.sent[0]
    assert args[:3] == ('role', 'account', 'target')
    assert kwargs['actor'] == 'reply' and kwargs['source_thread_id'] == 'source'
    assert kwargs['frozen_attachment_bytes'] and kwargs['source_run_id'] == 'run'


@pytest.mark.parametrize('revocation', ['source', 'material'])
def test_send_rechecks_immediately_before_dispatch(revocation):
    state = {'allowed': True}
    context, library, host, tools = setup(authorized=lambda: state['allowed'])
    call(tools, 'list', {}, context)
    host.before_send = lambda: state.update(allowed=False) if revocation == 'source' else setattr(library, 'current', False)
    assert call(tools, 'send', {'candidate_id': 'candidate'}, context).is_error
    assert not host.sent


def test_cross_run_and_revoked_owner_are_rejected():
    context, library, host, tools = setup(authorized=lambda: False)
    assert call(tools, 'list', {}, context).is_error
    assert not host.sent and not library.saved
    context, _, _, tools = setup()
    other = ToolContext('role', 'thread:source', 'other-run', CancellationToken())
    assert call(tools, 'list', {}, other).is_error


@pytest.mark.parametrize('permissions', [set(), {'read_file'}, {'account_send'}, {'write_file'}])
def test_send_never_borrows_missing_read_or_send_permission(permissions):
    context, _, host, tools = setup(permissions=permissions)
    call(tools, 'list', {}, context)
    assert call(tools, 'send', {'candidate_id': 'candidate'}, context).is_error
    assert host.sent == []


def test_uncertain_send_remains_unresolved_after_relisting():
    context, _, host, tools = setup()
    call(tools, 'list', {}, context)
    host.status = 'uncertain'
    receipt = call(tools, 'send', {'candidate_id': 'candidate'}, context)
    call(tools, 'list', {}, context)
    assert call(tools, 'send', {'candidate_id': 'candidate'}, context).content == receipt.content
    assert len(host.sent) == 1


def test_unavailable_read_permission_does_not_open_image_during_tool_preparation():
    _, library, _, _ = setup(attachments=[{'id': 'a', 'kind': 'image'}], permissions={'write_file'})
    assert library.frozen == []
