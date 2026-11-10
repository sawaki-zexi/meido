"""Joint #343/#347 real intake and tool-loop coverage, with only fake services."""
import asyncio
from dataclasses import replace
import json

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from test_novelai_management import management  # noqa: F401
from test_natural_scheduler import Clock


@pytest.mark.parametrize('natural_first', [True, False], ids=['natural-to-explicit', 'explicit-to-natural'])
@pytest.mark.parametrize('revoked', [False, True], ids=['same-owner', 'repaired-owner'])
def test_image_management_crosses_real_natural_and_explicit_turns(management, monkeypatch, natural_first, revoked):
    from backend.app.channels.natural import NaturalLimits
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
    from backend.app.novelai_generation.tools import ManageImageTool

    m, ids, results, observations = management, {}, [], []
    m.service.peer.block = True
    clock = Clock()
    monkeypatch.setattr(m.main, '_natural_scheduler', None)
    monkeypatch.setattr(m.main, '_schedule_clock', clock)
    scheduler = m.main._natural_application()
    scheduler.sleep = clock.sleep
    scheduler.limits = replace(NaturalLimits(), coalesce=1, min_role_interval=0)
    m.channels.listening.clock = clock
    m.channels.set_group_listening(m.service.role.id, m.account.account_id, '456', True)
    m.channels.listening.set_natural_enabled(m.service.role.id, m.thread.thread_id, True)
    execute = ManageImageTool.execute

    async def observed_execute(self, arguments, context, on_update=None):
        result = await execute(self, arguments, context, on_update)
        results.append((arguments['action'], result))
        return result

    monkeypatch.setattr(ManageImageTool, 'execute', observed_execute)

    class Model:
        phase = 'generate'
        natural = natural_first
        count = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            observations.extend(item for item in messages if item['role'] == 'tool')
            if self.natural and self.count == 1:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': '171' if self.phase == 'generate' else '172', 'intent': '继续当前图片任务'}}))
                return
            step = self.count - int(self.natural)
            if self.phase == 'generate' and step == 1:
                assert 'generate_image' in {item['function']['name'] for item in tools}
                yield ModelToolCall('create-image', 'generate_image', {**m.service.request, 'background': True})
            elif self.phase == 'manage' and step in (1, 2):
                assert 'manage_image' in {item['function']['name'] for item in tools}
                action = 'get' if step == 1 else 'cancel'
                yield ModelToolCall('manage-' + action, 'manage_image', {**ids, 'action': action})
            else:
                yield ModelTextDelta(json.dumps({'text': '已读取实际工具结果。'}) if self.natural else '已读取实际工具结果。')
                return
            yield ModelResponseDone('tool_use')

    model = Model()
    monkeypatch.setattr(m.main, 'model_adapter', model)

    async def inbound(identifier, natural):
        if not natural:
            await m.inbound(identifier, '继续这次图片任务')
            return
        await m.main._dispatch_channel_payload(m.account.account_id, {
            'post_type': 'message', 'message_type': 'group', 'self_id': 999, 'group_id': 456,
            'user_id': 123, 'message_id': identifier, 'sender': {'user_id': 123, 'nickname': 'fixture owner'},
            'message': [{'type': 'text', 'data': {'text': '继续这次图片任务'}}],
        }, source_adapter=m.main.channel_adapters[m.account.account_id])
        await asyncio.sleep(0)
        pending = tuple(scheduler.tasks.values())
        assert pending
        await clock.advance(1)
        await asyncio.wait_for(asyncio.gather(*pending), 12)

    async def scenario():
        try:
            await inbound(171, natural_first)
            accepted = json.loads(next(item['content'] for item in observations if item.get('tool_call_id') == 'create-image'))
            assert accepted['status'] == 'accepted'
            ids.update(generation_id=accepted['generationId'], attempt_id=accepted['attemptId'])
            assert await asyncio.to_thread(m.service.peer.accepted.wait, 2)
            if revoked:
                binding = next(item for item in m.channels.list_identities() if item['platformUserId'] == '123')
                m.channels.unbind_identity(binding['identityId'])
                m.pair()
            model.phase, model.natural, model.count = 'manage', not natural_first, 0
            await inbound(172, model.natural)
            assert [action for action, _ in results] == ['get', 'cancel']
            if revoked:
                for _, result in results:
                    assert result.is_error
                    assert result.details == {'status': 'unavailable', 'errorCode': 'generation_management_unavailable'}
                    assert accepted['taskId'] not in result.content
            else:
                queried, cancelled = (result for _, result in results)
                assert not queried.is_error and not cancelled.is_error
                assert queried.details['generationId'] == accepted['generationId']
                assert queried.details['taskId'] == accepted['taskId']
                assert queried.details['taskStatus'] in {'running', 'succeeded'}
                assert cancelled.details['attemptId'] == accepted['attemptId']
                assert cancelled.details['deliveryStatus'] == 'suppressed'
                assert cancelled.details['taskStatus'] in {'cancelled', 'succeeded'}
                assert cancelled.details['consumption'] == 'possible'
            assert any(item.get('tool_call_id') == 'manage-get' for item in observations)
            assert any(item.get('tool_call_id') == 'manage-cancel' for item in observations)
            m.service.peer.release.set()
            await m.service.application.recover()
            await m.service.application.recover()
            assert len(m.service.peer.requests) == 1
            assert not any(part.get('type') == 'image' for send in m.sends for part in send['message'])
            rows = m.service.sessions.list_messages(m.thread.session_key)
            assert sum(row.role == 'user' for row in rows) == 2
            assert all(send['group_id'] == 456 for send in m.sends)
        finally:
            m.service.peer.release.set()
            await scheduler.close()
            await m.service.application.close()
    asyncio.run(scenario())
