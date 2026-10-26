"""Natural text acceptance using real intake, runtime and NapCat dispatch."""
import asyncio
import json
from dataclasses import replace
import pytest
from test_meme_turn import meme_turn, payload
from test_natural_scheduler import Clock
from backend.app import main
from backend.app.channels.natural import NaturalLimits
from backend.app.model_adapter import ModelTextDelta, ModelToolCall

@pytest.fixture
def natural(meme_turn,monkeypatch):
    s=meme_turn
    from backend.app.channels import natural_media
    async def inline_worker(function,*args): return function(*args)
    monkeypatch.setattr(natural_media,'_owned_worker',inline_worker)
    s.clock=Clock(); s.calls=[]; s.result={'action':'reply','text':'你好'}; s.block=None
    class Model:
        async def stream_structured_messages(self,messages,configuration,tools):
            s.calls.append((messages,tools))
            if s.block: await s.block.wait()
            if getattr(s,'tool',False): yield ModelToolCall('fake','shell',{'command':'unsafe'})
            else: yield ModelTextDelta(json.dumps(s.result,ensure_ascii=False))
    monkeypatch.setattr(main,'model_adapter',Model())
    monkeypatch.setattr(main,'_natural_scheduler',None)
    monkeypatch.setattr(main,'_schedule_clock',s.clock)
    scheduler=main._natural_application()
    scheduler.sleep=s.clock.sleep
    scheduler.limits=NaturalLimits(coalesce=1,wait=2,lifetime=10,cooldown=3,min_role_interval=0)
    s.scheduler=scheduler
    s.channels.listening.clock=s.clock
    s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',True)
    s.thread=s.channels.get_thread_by_chat(s.role.id,s.adapter.account_id,'100',chat_type='group')
    s.channels.listening.set_natural_enabled(s.role.id,s.thread.thread_id,True)
    async def receive(id,**kwargs):
        data=payload(id,image=False,triggered=False,**kwargs)
        event=await main.channel_host.accept_event(s.adapter.account_id,data)
        if event is not None:
            await main._handle_channel_event(event)
        await asyncio.sleep(0)
    s.receive=receive
    return s


@pytest.mark.parametrize('sender,owner', [('11', True), ('22', False)])
def test_natural_model_receives_host_verified_sender_identity(natural, sender, owner):
    s = natural
    async def run():
        try:
            await s.receive('identity', sender=sender)
            await s.clock.advance(1)
            assert len(s.calls) == 1
            inputs = [json.loads(item['content']) for item in s.calls[0][0] if item['role'] == 'user']
            message = next(item for item in inputs if item['messageId'] == 'identity')
            assert message['senderIsOwner'] is owner
            assert not s.calls[0][1]
        finally:
            await s.scheduler.close()
    asyncio.run(run())


def test_natural_quote_names_owner_separately_from_guest(natural, monkeypatch):
    s = natural
    original = s.adapter._transport
    async def transport(operation, params):
        if operation == 'get_msg':
            return {'data': {'group_id': 100, 'message_type': 'group',
                'sender': {'user_id': 11, 'nickname': 'Owner'},
                'message': [{'type': 'text', 'data': {'text': '明天去旅行'}}]}}
        return await original(operation, params)
    monkeypatch.setattr(s.adapter, '_transport', transport)
    async def run():
        try:
            data = payload('natural-quote', sender='22', image=False, triggered=False)
            data['message'].insert(0, {'type': 'reply', 'data': {'id': 'owner-message'}})
            event = await main.channel_host.accept_event(s.adapter.account_id, data)
            await main._handle_channel_event(event)
            await asyncio.sleep(0)
            await s.clock.advance(1)
            inputs = [json.loads(item['content']) for item in s.calls[0][0] if item['role'] == 'user']
            message = next(item for item in inputs if item['messageId'] == 'natural-quote')
            assert message['senderIsOwner'] is False
            assert message['replyToSenderIdentity'] == '已配对主人'
            assert message['replyToContent'] == '明天去旅行'
            assert not s.calls[0][1]
        finally:
            await s.scheduler.close()
    asyncio.run(run())


def test_disabling_group_invalidates_pending_natural_reply_and_keeps_listening(natural):
    s = natural
    async def run():
        try:
            await s.receive('before-disable')
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, '100', False)
            await s.receive('while-disabled')
            await s.clock.advance(1)
            assert s.calls == [] and s.operations == []
            assert len(s.channels.listening.recent(s.role.id, s.thread.thread_id)) == 2
            s.channels.set_group_replies(s.role.id, s.adapter.account_id, '100', True)
            await s.clock.advance(10)
            assert s.calls == [] and s.operations == []
            await s.receive('new-after-enable')
            await s.clock.advance(1)
            assert len(s.calls) == 1 and len(s.operations) == 1
        finally:
            await s.scheduler.close()
    asyncio.run(run())


def test_natural_reply_scoped_real_message_and_no_tools(natural):
    s=natural
    async def run():
        await s.receive('1'); await s.clock.advance(1)
        assert len(s.calls)==1
        assert s.calls[0][1]==[] or s.calls[0][1]==()
        assert len(s.operations)==1
        op,params=s.operations[0]
        assert op=='send_group_msg' and params['group_id']==100
        assert [item['type'] for item in params['message']]==['text']
        messages=s.sessions.list_messages(s.thread.session_key)
        assert len(messages)==1 and messages[0].role=='assistant'
        assert messages[0].metadata['naturalReply'] is True
        assert messages[0].metadata['deliveryStatus']=='sent'
        assert messages[0].metadata['relatedInputIds']==['1']
        assert len(s.channels.listening.page(s.role.id,s.thread.thread_id)['messages'])==1
    asyncio.run(run())

@pytest.mark.parametrize('action',['wait','silence'])
def test_no_empty_records_for_wait_or_silence(natural,action):
    s=natural; s.result={'action':action}
    async def run():
        await s.receive('1'); await s.clock.advance(1)
        for _ in range(5): await s.clock.advance(2)
        assert not s.sessions.list_messages(s.thread.session_key)
        assert not s.operations
        assert len(s.calls)==(3 if action=='wait' else 1)
    asyncio.run(run())

@pytest.mark.parametrize('change',['natural','listening','clear','epoch','account'])
def test_unsent_result_invalidated(natural,change):
    s=natural
    async def run():
        s.block=asyncio.Event()
        await s.receive('1'); await s.clock.advance(1)
        assert s.calls
        if change=='natural': s.channels.listening.set_natural_enabled(s.role.id,s.thread.thread_id,False)
        elif change=='listening': s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',False)
        elif change=='clear': s.channels.listening.clear(s.role.id,s.thread.thread_id)
        elif change=='epoch': main.channel_host.unregister(s.adapter.account_id); main.channel_host.register(s.adapter)
        else: s.channels.set_account_state(s.adapter.account_id,enabled=False)
        s.block.set(); await s.clock.advance(0)
        assert not s.operations and not s.sessions.list_messages(s.thread.session_key)
    asyncio.run(run())


def test_quota_overflow_window_and_echo_guards(natural):
    s=natural
    async def run():
        s.channels.listening.set_cap(s.role.id,s.thread.thread_id,1)
        await s.receive('1'); await s.receive('2')
        await s.clock.advance(1)
        assert len(s.calls)==1 and len(s.operations)==1
        text=str(s.calls[0][0])
        assert '"messageId": "2"' in text
        assert len(s.channels.listening.page(s.role.id,s.thread.thread_id)['messages'])==1
        await s.clock.advance(4)
        await s.receive('3',sender='99'); await s.clock.advance(1)
        assert len(s.calls)==1
    asyncio.run(run())


def test_forged_tool_call_never_executes_or_sends(natural):
    s=natural; s.tool=True
    async def run():
        await s.receive('1'); await s.clock.advance(1)
        assert len(s.calls)==1 and not s.operations
        assert not s.sessions.list_messages(s.thread.session_key)
    asyncio.run(run())

@pytest.mark.parametrize('outcome',['sent','failed','uncertain'])
def test_pending_and_real_delivery_receipt(natural,outcome):
    from backend.app.channels.base import PlatformRejectedError
    s=natural
    async def run():
        entered=asyncio.Event(); release=asyncio.Event()
        async def transport(operation,params):
            s.operations.append((operation,params)); entered.set(); await release.wait()
            if outcome=='failed': raise PlatformRejectedError('controlled')
            return {'message_id':'9000'} if outcome=='sent' else {}
        s.adapter._transport=transport
        await s.receive('1'); await s.clock.advance(1)
        await entered.wait()
        messages=s.sessions.list_messages(s.thread.session_key)
        assert len(messages)==1 and messages[0].metadata['deliveryStatus']=='pending'
        s.channels.listening.set_natural_enabled(s.role.id,s.thread.thread_id,False)
        release.set(); await s.clock.advance(0)
        messages=s.sessions.list_messages(s.thread.session_key)
        assert len(messages)==1 and messages[0].metadata['deliveryStatus']==outcome
        assert messages[0].status==('completed' if outcome=='sent' else 'failed')
        assert len(s.operations)==1
        # Reopening never replays. Next prompt includes only confirmed sent output.
        s.channels.listening.set_natural_enabled(s.role.id,s.thread.thread_id,True)
        await s.clock.advance(4)
        s.result={'action':'silence'}
        await s.receive('2',sender='22'); await s.clock.advance(1)
        system=s.calls[-1][0][0]['content']
        assert ('你好' in system)==(outcome=='sent')
        assert len(s.operations)==1
    asyncio.run(run())

@pytest.mark.parametrize('quoted',[False,True])
def test_explicit_at_or_quote_preempts_natural_and_preserves_tools(natural,quoted):
    s=natural
    async def run():
        s.block=asyncio.Event()
        await s.receive('1'); await s.clock.advance(1)
        assert len(s.calls)==1
        event=await main.channel_host.accept_event(s.adapter.account_id,payload('explicit',image=False,triggered=True,quoted=quoted))
        task=asyncio.create_task(main._handle_channel_event(event))
        for _ in range(20): await asyncio.sleep(0)
        s.block.set()
        await task
        assert len(s.calls)==2 and s.calls[1][1]
        messages=s.sessions.list_messages(s.thread.session_key)
        assert [m for m in messages if m.role=='user']
        assert not any(m.metadata.get('naturalReply') for m in messages)
        assert len([op for op,_ in s.operations if op.startswith('send_')])==1
        assert not s.scheduler.batches
    asyncio.run(run())


def test_saved_switch_recovers_only_new_input_and_context_isolation(natural):
    s=natural
    other=s.channels.ensure_channel_thread(s.role.id,'qq',s.adapter.account_id,'200','group')
    private=s.channels.ensure_channel_thread(s.role.id,'qq',s.adapter.account_id,'11','private')
    for thread in [other,private]:
        s.channels.append_event(thread,direction='inbound',content='PRIVATE_SECRET',external_message_id=thread.thread_id)
    async def run():
        s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',False)
        assert s.channels.listening.state(s.role.id,s.thread.thread_id)['naturalPaused']
        await s.receive('ignored')
        s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',True)
        await s.clock.advance(3)
        assert not s.calls
        await s.receive('new'); await s.clock.advance(1)
        assert s.calls and 'PRIVATE_SECRET' not in str(s.calls)
        assert 'ignored' not in str(s.calls)
        assert not s.channels.listening.state(s.role.id,other.thread_id)['naturalEnabled']
    asyncio.run(run())


def test_bot_marker_and_duplicate_input_do_not_wake(natural):
    s=natural
    async def run():
        data=payload('bot',image=False,triggered=False); data['sender']['is_bot']=True
        event=await main.channel_host.accept_event(s.adapter.account_id,data)
        await main._handle_channel_event(event); await s.clock.advance(1)
        assert not s.calls
        await s.receive('1'); await s.receive('1'); await s.clock.advance(1)
        assert len(s.calls)==1
        assert s.sessions.list_messages(s.thread.session_key)[0].metadata['relatedInputIds']==['1']
    asyncio.run(run())


def test_restart_projects_pending_as_uncertain_without_replay(natural):
    s=natural
    attempt=s.channels.begin_delivery(s.thread,'candidate',source_kind='natural')
    s.channels.append_event(s.thread,direction='outbound',content='candidate',external_message_id=None,
        role='assistant',status='pending',metadata={'naturalReply':True,'deliveryStatus':'pending','deliveryAttemptId':attempt})
    assert s.channels.mark_interrupted_deliveries_uncertain()==1
    message=s.sessions.list_messages(s.thread.session_key)[0]
    assert message.metadata['deliveryStatus']=='uncertain' and message.status=='failed'
    assert not s.operations and not s.scheduler.batches


def test_old_transport_event_cannot_adopt_replacement_epoch(natural):
    s=natural
    async def run():
        old=main.channel_host.connection_generation(s.adapter.account_id)
        event=await main.channel_host.accept_event(s.adapter.account_id,payload('old',image=False,triggered=False))
        event=replace(event,metadata={**event.metadata,'hostConnectionGeneration':old})
        main.channel_host.unregister(s.adapter.account_id); main.channel_host.register(s.adapter)
        await main._handle_channel_event(event); await s.clock.advance(1)
        assert not s.calls and not s.operations
    asyncio.run(run())

@pytest.mark.parametrize('permission',['group','sender'])
def test_revoked_group_or_sender_permission_blocks_unsent(natural,permission):
    s=natural
    async def run():
        s.block=asyncio.Event()
        await s.receive('1'); await s.clock.advance(1)
        assert s.calls
        with s.channels._connect() as db:
            if permission=='group': db.execute('UPDATE channel_accounts SET allow_group=0 WHERE account_id=?',(s.adapter.account_id,))
            else: db.execute('UPDATE channel_accounts SET blocked_senders_json=? WHERE account_id=?',(json.dumps(['11']),s.adapter.account_id))
        s.block.set(); await s.clock.advance(0)
        assert not s.operations
    asyncio.run(run())


def test_role_deletion_rollback_preserves_setting_and_spent_quota(natural):
    from test_role_deletion_recovery import Credentials
    s=natural
    s.channels.credentials=Credentials()
    assert s.scheduler.quota.take(s.role.id,1000,daily=1,interval=0)
    snapshot=s.channels.snapshot_role(s.role.id)
    s.channels.delete_role(s.role.id)
    s.channels.restore_role(s.role.id,snapshot)
    assert s.channels.listening.state(s.role.id,s.thread.thread_id)['naturalEnabled']
    assert not s.scheduler.quota.take(s.role.id,1001,daily=1,interval=0)

@pytest.mark.parametrize('change',['cycle','clear'])
def test_prechange_deferred_input_cannot_repopulate_or_reply(natural,change):
    s=natural
    async def run():
        event=await main.channel_host.accept_event(s.adapter.account_id,payload('old',image=False,triggered=False))
        event=replace(event,received_at=s.clock())
        await s.clock.advance(.1)
        if change=='cycle':
            s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',False)
            s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',True)
        else: s.channels.listening.clear(s.role.id,s.thread.thread_id)
        await main._handle_channel_event(event); await s.clock.advance(1)
        assert not s.calls and not s.operations
        assert not s.channels.listening.recent(s.role.id,s.thread.thread_id)
    asyncio.run(run())


def test_cancelled_preemption_waiter_preserves_inflight_receipt(natural):
    s=natural
    async def run():
        entered=asyncio.Event(); release=asyncio.Event()
        async def transport(operation,params):
            entered.set(); await release.wait(); return {'message_id':9001}
        s.adapter._transport=transport
        await s.receive('1'); await s.clock.advance(1); await entered.wait()
        waiter=asyncio.create_task(s.scheduler.preempt(s.thread.thread_id))
        await asyncio.sleep(0); waiter.cancel(); await asyncio.sleep(0); waiter.cancel()
        release.set()
        await asyncio.gather(waiter,return_exceptions=True)
        assert s.sessions.list_messages(s.thread.session_key)[0].metadata['deliveryStatus']=='sent'
        assert not s.scheduler.tasks
    asyncio.run(run())


def test_forced_dispatch_cancellation_is_uncertain_not_pending(natural):
    s=natural
    async def run():
        entered=asyncio.Event()
        async def transport(operation,params): entered.set(); await asyncio.Event().wait()
        s.adapter._transport=transport
        await s.receive('1'); await s.clock.advance(1); await entered.wait()
        task=s.scheduler.tasks[s.thread.thread_id]; task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert s.sessions.list_messages(s.thread.session_key)[0].metadata['deliveryStatus']=='uncertain'
        assert s.channels.list_deliveries(s.role.id)[0]['status']=='uncertain'
    asyncio.run(run())


def test_natural_setting_route_validates_owner_scoped_group(natural):
    from fastapi.testclient import TestClient
    s=natural
    base=f'/api/roles/{s.role.id}/channels/threads/{s.thread.thread_id}/listening/natural'
    client=TestClient(main.app)
    assert client.put(base,json={'enabled':'yes'}).status_code==422
    assert client.put(base,json={'enabled':False}).json()['naturalEnabled'] is False
    s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',False)
    assert client.put(base,json={'enabled':True}).status_code==422
    s.channels.set_group_listening(s.role.id,s.adapter.account_id,'100',True)
    assert client.put(base,json={'enabled':True}).json()['naturalEffective'] is True
    assert client.put(base.replace(s.thread.thread_id,'other-thread'),json={'enabled':True}).status_code==404


def test_clear_between_hear_and_schedule_cannot_resurrect(natural,monkeypatch):
    s=natural; original=s.channels.listening.hear
    def clearing_hear(*args,**kwargs):
        value=original(*args,**kwargs)
        s.channels.listening.clear(s.role.id,s.thread.thread_id)
        return value
    monkeypatch.setattr(s.channels.listening,'hear',clearing_hear)
    async def run():
        await s.receive('1'); await s.clock.advance(1)
        assert not s.calls and not s.operations
        assert not s.channels.listening.recent(s.role.id,s.thread.thread_id)
    asyncio.run(run())


def test_cancelled_explicit_preemption_waiter_is_terminal(natural):
    s=natural
    async def run():
        entered=asyncio.Event(); release=asyncio.Event()
        async def transport(operation,params):
            entered.set(); await release.wait(); return {'message_id':9002}
        s.adapter._transport=transport
        await s.receive('1'); await s.clock.advance(1); await entered.wait()
        event=await main.channel_host.accept_event(s.adapter.account_id,payload('explicit',image=False,triggered=True))
        task=asyncio.create_task(main._handle_channel_event(event))
        for _ in range(10): await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0); release.set()
        await asyncio.gather(task,return_exceptions=True)
        user=next(m for m in s.sessions.list_messages(s.thread.session_key) if m.role=='user')
        assert user.metadata['channelTurnState']=='cancelled'
        assert s.channels.list_deliveries(s.role.id)[0]['status']=='sent'
    asyncio.run(run())
