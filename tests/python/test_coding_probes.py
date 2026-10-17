from pathlib import Path
import sys
import threading
import time

import pytest

from backend.app.coding_tasks import probes
from backend.app.coding_tasks.owned_process import CleanupFailed, OwnedProcess
from backend.app.task_runtime import TaskRuntime, TaskOutcome

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_failed_probe_cleanup_retains_actual_process_owner_until_retry(tmp_path, monkeypatch):
    profile = {'id': 'profile', 'revision': 'revision', 'kind': 'codex', 'version': '0.159.3',
               'executor': 'herdr', 'executable': sys.executable, 'sha256': 'fixture', 'allowedRoleIds': ['role']}
    build = {'executable': sys.executable, 'sha256': 'fixture-build'}
    class Configuration:
        def profile(self, identifier): return profile
        def native_build(self): return build
        def role_generation(self, role_id): return 0
    monkeypatch.setattr(probes, 'executable_identity', lambda *args: None)
    monkeypatch.setattr(probes, 'verify_bundle', lambda *args: None)
    release = threading.Event()
    owners = []
    class ControlledQualification:
        def __init__(self, service, record, profile):
            self.service, self.record, self.process = service, record, None
            owners.append(self)
        def run(self, execution):
            self.process = OwnedProcess([sys.executable, '-I', '-c', 'import time; time.sleep(600)'], tmp_path, {})
            self.service.update(self.record['id'], status='blocked', cleanupState='cleanup_failed')
            return TaskOutcome('failed', None, 'cleanup pending')
        def cancel(self):
            if not release.is_set(): raise CleanupFailed('controlled transient close failure')
            self.process.close()
    monkeypatch.setattr(probes, 'QualificationExecutor', ControlledQualification)
    runtime = TaskRuntime(tmp_path / 'runtime')
    service = probes.NativeProfileProbes(tmp_path, runtime, Configuration(), role_exists=lambda _: True)
    try:
        accepted = service.start('profile', revision='revision', role_id='role', build_sha256='fixture-build',
                                 approve_native_turns=2, request_id='owner-action')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            service.tick()
            observed = service.get(accepted['id'])
            if observed.get('runtimeResult'): break
            time.sleep(.01)
        assert observed['runtimeAckDirty'] is False
        assert owners[0].process.process.poll() is None
        assert service._owners[accepted['id']] is owners[0]
        with pytest.raises(ValueError, match='active|cleanup'):
            service.start('profile', revision='revision', role_id='role', build_sha256='fixture-build',
                          approve_native_turns=2, request_id='another-action')
        release.set()
        service.tick()
        assert owners[0].process.process.poll() is not None
        assert service.get(accepted['id'])['cleanupState'] == 'complete'
        assert accepted['id'] not in service._owners
    finally:
        release.set()
        for owner in owners:
            if owner.process is not None: owner.process.close()
        service.close()
        runtime.close()
