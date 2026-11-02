import asyncio
import threading

import pytest

from backend.app.host_memory_optimizer import HostMemoryOptimizer
from backend.app.memory_optimizer import MemoryOptimizerWorker


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_startup_eligibility_uses_current_self_rules_and_elapsed_interval(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone
    folder = tmp_path / 'r' / 'memory'
    folder.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (folder / 'PENDING.md').write_text('# 待整理的记忆\n- next periodic candidate', encoding='utf-8')
    state = {'self_rules_version': 2, 'last_memory_optimized_at': now.isoformat()}
    (folder / '.optimizer.json').write_text(json.dumps(state), encoding='utf-8')
    assert not HostMemoryOptimizer.should_run(tmp_path, 'r', now=now, interval_seconds=60)
    assert HostMemoryOptimizer.should_run(tmp_path, 'r', now=now + timedelta(seconds=60), interval_seconds=60)
    state['self_rules_version'] = 1
    (folder / '.optimizer.json').write_text(json.dumps(state), encoding='utf-8')
    assert HostMemoryOptimizer.should_run(tmp_path, 'r', now=now, interval_seconds=60)


@pytest.mark.anyio
async def test_cancel_restores_snapshot_and_new_arrivals_before_model_returns(tmp_path):
    folder = tmp_path / 'r/memory'
    folder.mkdir(parents=True)
    pending = folder / 'PENDING.md'
    pending.write_text('- [identity] 原有事实\n', encoding='utf-8')
    started, release = threading.Event(), threading.Event()

    def request(*args):
        started.set()
        release.wait(3)
        return '# 我的长期记忆\n## 关于你\n- 旧任务结果'

    worker = MemoryOptimizerWorker(HostMemoryOptimizer(tmp_path, request, step_delay_seconds=0))
    worker.submit('r')
    assert await asyncio.to_thread(started.wait, 1)
    pending.write_text('- [preference] 新到达事实\n', encoding='utf-8')
    try:
        await asyncio.wait_for(worker.cancel_generation(), .5)
        assert '原有事实' in pending.read_text(encoding='utf-8')
        assert '新到达事实' in pending.read_text(encoding='utf-8')
        assert not (folder / 'PENDING.snapshot.md').exists()
        assert not (folder / 'MEMORY.md').exists()
    finally:
        release.set()
    await worker.close()
    assert not (folder / 'MEMORY.md').exists()


@pytest.mark.anyio
async def test_merge_failure_still_updates_self_and_empty_current_version_skips(tmp_path):
    folder = tmp_path / 'r/memory'
    folder.mkdir(parents=True)
    (folder / 'PENDING.md').write_text('- [identity] 事实', encoding='utf-8')
    calls = []
    def request(role, system, prompt, tokens):
        calls.append(tokens)
        if tokens == 16384:
            raise RuntimeError('controlled merge failure')
        return '# 我是谁\n## 我的性格与形象\n- 温和\n## 我对你的理解\n## 我们的关系\n'
    optimizer = HostMemoryOptimizer(tmp_path, request, step_delay_seconds=0)
    assert await optimizer.optimize('r')
    assert calls == [16384, 2048]
    assert '事实' in (folder / 'PENDING.md').read_text(encoding='utf-8')
    (folder / 'PENDING.md').write_text('', encoding='utf-8')
    calls.clear()
    assert not await optimizer.optimize('r')
    assert calls == []


@pytest.mark.anyio
async def test_cancel_after_memory_commit_preserves_memory_without_restoring_consumed_pending(tmp_path):
    folder = tmp_path / 'r/memory'
    folder.mkdir(parents=True)
    (folder / 'PENDING.md').write_text('- [identity] 已合并事实', encoding='utf-8')
    optimizer = HostMemoryOptimizer(tmp_path, lambda *_: '# 我的长期记忆\n## 关于你\n- 已合并事实', step_delay_seconds=30)
    task = asyncio.create_task(optimizer.optimize('r'))
    for _ in range(100):
        if (folder / 'MEMORY.md').exists():
            break
        await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert '已合并事实' in (folder / 'MEMORY.md').read_text(encoding='utf-8')
    assert not (folder / 'PENDING.md').read_text(encoding='utf-8').strip()
    assert not (folder / 'PENDING.snapshot.md').exists()
