"""Admission failure must not leave a late real Windows mutex owner."""
import json
import subprocess
import sys
from uuid import uuid4

import pytest

pytestmark=[pytest.mark.skipif(sys.platform!="win32",reason="real Windows mutex"),
            pytest.mark.usefixtures("local_platform_only")]


def test_readiness_timeout_releases_and_joins_late_mutex_admission():
    # The child owns a unique mutex, no desktop/input process. Process exit
    # releases all handles even when verifying the old broken implementation.
    program=r'''
import json,sys,threading,time
from backend.app.computer_use import lease
from backend.app.browser_use.process import WindowsJob
lease.MUTEX_NAME=sys.argv[1]
lease.JOB_NAME=sys.argv[1]+".Job"
original=WindowsJob.named_job_active
first=True
def delayed(name):
    global first
    if first:
        first=False
        time.sleep(5.3)
    return original(name)
WindowsJob.named_job_active=delayed
result={}
try:
    owner=lease.DesktopLease()
    owner.close()
    result['initial']='unexpected success'
except Exception as error:
    result['initial']=type(error).__name__
time.sleep(.4)
result['remaining_threads']=sum(t.name=='meido-computer-lease' for t in threading.enumerate())
try:
    next_owner=lease.DesktopLease()
    next_owner.close()
    result['next']='admitted'
except Exception as error:
    result['next']=getattr(error,'code',type(error).__name__)
print(json.dumps(result),flush=True)
'''
    result=subprocess.run([sys.executable,"-c",program,"Local\\Meido169.Readiness."+uuid4().hex],
        capture_output=True,text=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr
    observed=json.loads(result.stdout)
    assert observed["remaining_threads"]==0,observed
    assert observed["next"]=="admitted",observed
