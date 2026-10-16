import os
from pathlib import Path
import sys
import time
import subprocess
import uuid
import threading

import pytest

from backend.app.coding_tasks.owned_process import OwnedProcess

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_owned_graceful_request_is_bounded_and_always_followed_by_confirmed_cleanup(tmp_path):
    process = OwnedProcess([sys.executable, '-I', '-c', 'import time;time.sleep(30)'], tmp_path, {})
    try:
        started = time.monotonic()
        accepted = process.request_stop(grace_seconds=.1)
        assert isinstance(accepted, bool)
        assert time.monotonic() - started < 1
        # A no-console Windows child may reject CTRL_BREAK. This cannot be
        # called graceful success; the following Job close is authoritative.
        process.close()
        assert process.process.poll() is not None and process.cleanup_state == 'complete'
    finally:
        process.close()


def test_unread_stdin_cannot_block_deadline_and_writer_is_joined(tmp_path):
    process = OwnedProcess([sys.executable, '-I', '-c', 'import time;time.sleep(30)'], tmp_path, {})
    outcome = []
    def communicate():
        try:
            process.communicate(timeout=.2, input=b'x' * (1024 * 1024))
        except BaseException as error:
            outcome.append(error)
    worker = threading.Thread(target=communicate)
    worker.start()
    try:
        worker.join(timeout=2)
        assert not worker.is_alive(), 'stdin write detached the attempt deadline'
        assert isinstance(outcome[0], TimeoutError)
        assert process.cleanup_state == 'complete'
        assert not process._writer.is_alive()
    finally:
        process.close()
        worker.join(timeout=3)


def test_streamed_stdout_callback_receives_exact_bytes_and_rejects_before_process_exit(tmp_path):
    chunks = []
    def receive(chunk):
        chunks.append(chunk)
        raise ValueError('invalid protocol')
    process = OwnedProcess([sys.executable, '-I', '-u', '-c', 'import sys,time;sys.stdout.buffer.write(b"frame\\n");sys.stdout.flush();time.sleep(30)'],
                           tmp_path, {}, on_stdout=receive)
    started = time.monotonic()
    with pytest.raises(ValueError, match='invalid protocol'):
        process.communicate(timeout=10)
    assert time.monotonic() - started < 3
    assert b''.join(chunks) == b'frame\n'
    assert process.cleanup_state == 'complete'


def test_owned_process_captures_utf8_and_cleans_descendants_without_output_pipes(tmp_path):
    marker = tmp_path / 'late.txt'
    source = ('import subprocess,sys,time; '
              'subprocess.Popen([sys.executable,"-c",' + repr('import time,pathlib; time.sleep(1); pathlib.Path(' + repr(str(marker)) + ').write_text("escaped")') + '],'
              'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
              'print("原生任务🙂")')
    process = OwnedProcess([sys.executable, '-X', 'utf8', '-c', source], tmp_path,
                           {key: value for key, value in os.environ.items() if key in {'SystemRoot', 'WINDIR', 'TEMP', 'TMP'}})
    result = process.communicate(timeout=5, output_bytes=1024)
    assert result.returncode == 0
    assert result.stdout.decode('utf-8').strip() == '原生任务🙂'
    assert process.cleanup_state == 'complete'
    time.sleep(1.2)
    assert not marker.exists()
    process.close()
    assert process.cleanup_state == 'complete'


@pytest.mark.skipif(os.name != 'nt', reason='Windows named Job crash acceptance')
def test_host_crash_closes_named_job_before_restart_observes_it(tmp_path):
    from backend.app.coding_tasks.owned_process import named_job_active
    marker = tmp_path / 'escaped.txt'
    job = 'Local\\Meido.Coding.crash-' + uuid.uuid4().hex
    child = 'import time,pathlib; time.sleep(2); pathlib.Path(' + repr(str(marker)) + ').write_text("escaped")'
    source = ('import sys,os; from pathlib import Path; sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[2])) + '); '
        'from backend.app.coding_tasks.owned_process import OwnedProcess; '
        'owned=OwnedProcess([sys.executable,"-I","-c",' + repr(child) + '],Path(' + repr(str(tmp_path)) + '),{},job_name=' + repr(job) + '); '
        'os._exit(0)')
    # This host is intentionally not inside a second Job: an outer test Job
    # would hide a broken kill-on-close in the owned Job being verified.
    host = subprocess.run([sys.executable, '-I', '-c', source], cwd=tmp_path,
                          env={key: value for key, value in os.environ.items() if key in
                              {'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'MEIDO_DATA_DIR', 'MEIDO_ROLES_DIR', 'MEIDO_OWNER_KNOWLEDGE_DATA_DIR'}},
                          capture_output=True, timeout=10)
    assert host.returncode == 0, host.stderr.decode('utf-8', errors='replace')
    assert named_job_active(job) is False
    time.sleep(2.3)
    assert not marker.exists()
