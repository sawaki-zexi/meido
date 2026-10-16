import os
from pathlib import Path
import sys
import time
import subprocess
import uuid

import pytest

from backend.app.coding_tasks.owned_process import OwnedProcess

pytestmark = pytest.mark.usefixtures('local_platform_only')


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
