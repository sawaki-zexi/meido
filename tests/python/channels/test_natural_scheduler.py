import asyncio
import functools
from types import SimpleNamespace
import pytest
from backend.app.channels.natural import NaturalScheduler, NaturalInput, NaturalDecision, NaturalLimits, NaturalQuota
from backend.app.session_store import SessionStore
from backend.app.multichannel import MultichannelStore

def async_test(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs): return asyncio.run(fn(*args, **kwargs))
    return run

class Clock:
    def __init__(self): self.now = 1000.; self.waiters = []
    def __call__(self): return self.now
    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future
    async def advance(self, seconds):
        self.now += seconds
        for end, future in self.waiters:
            if end <= self.now and not future.done(): future.set_result(None)
        for _ in range(10): await asyncio.sleep(0)

@pytest.fixture
def setup(tmp_path):
    sessions = SessionStore(tmp_path/'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    clock = Clock(); sent=[]; calls=[]
    state = SimpleNamespace(clock=clock, sent=sent, calls=calls, valid=True, busy=False, decision=NaturalDecision('reply','hello'), block=None)
    async def decide(batch, inputs, authorized):
        calls.append(tuple(item.message_id for item in inputs))
        if state.block: await state.block.wait()
        return state.decision
    async def send(batch, decision, authorized):
        if authorized(): sent.append(decision.text)
    limits=NaturalLimits(coalesce=1, wait=2, lifetime=10, cooldown=3, max_calls=3, min_role_interval=0)
    state.scheduler=NaturalScheduler(decide=decide, send=send, authorized=lambda b: state.valid,
        busy=lambda role: state.busy, quota=NaturalQuota(channels), clock=clock, sleep=clock.sleep, limits=limits)
    state.thread=SimpleNamespace(thread_id='t',role_id='r',account_id='a')
    return state

def item(id, sender='u', **metadata): return NaturalInput(id,sender,'text',metadata)

async def start(state, message):
    state.scheduler.accept(state.thread,message)
    await asyncio.sleep(0)

@async_test
async def test_coalesces_and_does_not_self_continue(setup):
    s=setup
    await start(s,item('1')); await start(s,item('2'))
    await s.clock.advance(1)
    assert s.calls==[('1','2')] and s.sent==['hello']
    await s.clock.advance(100)
    assert len(s.calls)==1

@async_test
async def test_wait_is_bounded_no_reset(setup):
    s=setup; s.decision=NaturalDecision('wait')
    await start(s,item('1')); await s.clock.advance(1)
    for _ in range(5): await s.clock.advance(2)
    assert len(s.calls)==3 and not s.sent and not s.scheduler.batches

@async_test
async def test_related_redoes_unrelated_does_not(setup):
    s=setup; s.block=asyncio.Event()
    await start(s,item('1')); await s.clock.advance(1)
    await start(s,item('2','other'))
    s.block.set(); await s.clock.advance(0)
    assert s.calls==[('1',)] and s.sent==['hello']
    await s.clock.advance(4)
    s.block=asyncio.Event()
    await start(s,item('3')); await s.clock.advance(1)
    await start(s,item('4'))
    s.block.set(); await s.clock.advance(0)
    assert s.calls[-2:]==[('3',),('3','4')] and s.sent==['hello','hello']

@async_test
async def test_explicit_priority_and_invalidation_cancel_only_owned_task(setup):
    s=setup; s.block=asyncio.Event()
    await start(s,item('1')); await s.clock.advance(1)
    await s.scheduler.preempt('t')
    s.block.set(); await s.clock.advance(0)
    assert not s.sent and not s.scheduler.batches
    await start(s,item('2')); s.scheduler.invalidate('t'); await s.clock.advance(1)
    assert not s.sent

@async_test
async def test_busy_does_not_queue_runtime_and_expires(setup):
    s=setup; s.busy=True
    await start(s,item('1')); await s.clock.advance(11)
    assert not s.calls and not s.sent

@async_test
async def test_other_target_supplement_not_related(setup):
    s=setup; s.block=asyncio.Event()
    await start(s,item('1')); await s.clock.advance(1)
    await start(s,item('2',mentionedIds=['someone-else']))
    s.block.set(); await s.clock.advance(0)
    assert len(s.calls)==1 and s.sent

def test_quota_persists_across_restart(setup):
    quota=setup.scheduler.quota
    assert quota.take('r',1000,daily=2,interval=0)
    assert quota.take('r',1001,daily=2,interval=0)
    assert not NaturalQuota(quota.store).take('r',1002,daily=2,interval=0)
    assert quota.take('r',100000,daily=2,interval=0)

@async_test
async def test_cleanup_failure_releases_and_old_input_is_not_rejuvenated(setup):
    s=setup
    def bad_cleanup(batch): raise OSError('controlled')
    s.scheduler.cleanup=bad_cleanup
    await start(s,item('1')); await s.clock.advance(1)
    assert not s.scheduler.batches and not s.scheduler.tasks
    await s.clock.advance(4)
    await start(s,item('old',hostReceivedAt=s.clock()-20))
    await s.clock.advance(1)
    assert len(s.calls)==1

@async_test
async def test_reply_is_one_short_plain_message_even_for_long_multiline_model(setup):
    s=setup; s.decision=NaturalDecision('reply',('\n'.join(['word'*50]*30)))
    await start(s,item('1')); await s.clock.advance(1)
    assert len(s.sent)==1 and len(s.sent[0])<=280 and len(s.sent[0].splitlines())<=8

@async_test
async def test_shared_quoted_ancestor_invalidates_old_answer(setup):
    s=setup; s.block=asyncio.Event()
    await start(s,item('1',replyTo='ancestor')); await s.clock.advance(1)
    await start(s,item('2','other',replyTo='ancestor'))
    s.block.set(); await s.clock.advance(0)
    assert s.calls==[('1',),('1','2')]
