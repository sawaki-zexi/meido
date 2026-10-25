"""Real-image ordinary owner copies, isolated from all services and accounts."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading

from PIL import Image
import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.chat_attachments import save_attachment
from backend.app.channels.meme_library import ChatMemeLibrary


def picture(format='PNG', color='red'):
    output = io.BytesIO()
    image = Image.new('RGB', (4, 3), color)
    if format == 'GIF':
        image.save(output, format, save_all=True, append_images=[Image.new('RGB', (4, 3), 'blue')], duration=50, loop=0)
    else:
        image.save(output, format)
    return output.getvalue()


def real_library(base, managed=False):
    import sqlite3
    from backend.app.proactive_runtime.meme_library import MemeLibrary
    from backend.app.proactive_runtime.drift_workspace import FileLimits
    database=base/'isolated.db'
    base.mkdir(parents=True,exist_ok=True)
    @contextmanager
    def transaction():
        db=sqlite3.connect(database,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    with transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS autonomy_roles (role_id TEXT PRIMARY KEY)')
        db.execute("INSERT OR IGNORE INTO autonomy_roles VALUES ('alice')")
    root=base/'roles'
    (root/'alice').mkdir(parents=True,exist_ok=True)
    grant=SimpleNamespace(enabled=managed,managedEpoch='owner-managed-epoch' if managed else '')
    app=SimpleNamespace(_transaction=transaction,_gate=threading.RLock(),
        drift_skills=SimpleNamespace(workspace=lambda role:root/role,configuration=lambda role:SimpleNamespace(driftMeme=grant)),
        drift_files=SimpleNamespace(limits=FileLimits()),role_getter=lambda role:object())
    shared=MemeLibrary(app)
    if managed:
        with transaction() as db:
            shared.adopt(db,'alice',grant)
    return ChatMemeLibrary(shared,lambda role:root/role)


@pytest.fixture
def library(tmp_path):
    return real_library(tmp_path)


@pytest.fixture
def context():
    return SimpleNamespace(role_id='alice', session_key='thread:group', run_id='run', tool_call_id='copy', signal=CancellationToken())


def upload(library, format='PNG', color='red'):
    extension, media = {'PNG': ('png','image/png'), 'JPEG': ('jpg','image/jpeg'), 'WEBP': ('webp','image/webp'), 'GIF': ('gif','image/gif')}[format]
    return save_attachment(library.attachment_root('alice'), 'image.'+extension, media, picture(format,color))


@pytest.mark.parametrize('format', ['PNG','JPEG','WEBP','GIF'])
def test_copy_and_reference_preserve_actual_original_bytes(library,context,format):
    item = upload(library,format)
    result = library.save('alice',item,'happy','愉快的表情',context,lambda: True)
    assert result['status'] == 'recorded'
    candidate, = library.candidates('alice')
    metadata, raw = library.reference('alice',candidate,context)
    assert raw == Path(item['path']).read_bytes() == Path(metadata['path']).read_bytes()
    if format == 'GIF':
        assert Image.open(io.BytesIO(raw)).n_frames == 2
    assert library.reference('alice',candidate,context)[0]['id'] == metadata['id']
    assert len(list((library.attachment_root('alice')/'attachments').glob('*.json'))) == 2


def test_dedup_preserves_manifest_manual_edits_and_original(library,context):
    item=upload(library)
    first=library.save('alice',item,'happy','original',context,lambda: True)
    root=library.library.root('alice')
    manifest=root/'manifest.json'
    data=json.loads(manifest.read_text())
    data['categories']['happy']={'desc':'owner edited','aliases':['人工'],'enabled':False}
    manifest.write_text(json.dumps(data))
    second=library.save('alice',item,'happy','overwritten?',context,lambda: True)
    assert second['deduplicated']
    assert first['sha256']==second['sha256']
    assert json.loads(manifest.read_text()) == data
    assert len(list((root/'happy').glob('*.png'))) == 1
    assert Path(item['path']).exists()
    assert library.candidates('alice') == ()


def test_replaced_deleted_disabled_cross_role_candidates_rejected(library,context):
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    assert not library.valid('bob',candidate)
    candidate.path.write_bytes(picture(color='green'))
    assert not library.valid('alice',candidate)
    with pytest.raises((ValueError,PermissionError)):
        library.reference('alice',candidate,context)


def test_unregistered_corrupt_and_revoked_sources_do_not_save(library,context):
    item=upload(library)
    for invalid in ({**item,'id':'a'*32},{**item,'path':str(Path(item['path']).parent.parent/'other.png')}):
        with pytest.raises((ValueError,PermissionError)):
            library.save('alice',invalid,'happy','happy',context,lambda:True)
    with pytest.raises(PermissionError):
        library.save('alice',item,'happy','happy',context,lambda:False)
    Path(item['path']).write_bytes(b'\x89PNG\r\n\x1a\nbroken')
    with pytest.raises((ValueError,PermissionError,OSError)):
        library.save('alice',item,'happy','happy',context,lambda:True)
    assert library.candidates('alice') == ()


def test_legacy_enabled_library_needs_no_attachment_registration(library,context):
    root=library.library.root('alice')
    (root/'joy').mkdir(parents=True)
    (root/'manifest.json').write_text(json.dumps({'version':1,'categories':{'joy':{'desc':'joyful','aliases':['开心'],'enabled':True}}}))
    (root/'joy'/'old.png').write_bytes(picture())
    candidate,=library.candidates('alice','开心')
    assert candidate.description=='joyful'
    assert library.reference('alice',candidate,context)[1] == picture()
    assert len(list((library.attachment_root('alice')/'attachments').glob('*.json'))) == 1


def test_original_frozen_input_cannot_be_replaced_before_tool_call(library, context):
    item=upload(library)
    frozen=library.freeze_attachment('alice',item)
    Path(item['path']).write_bytes(picture(color='blue'))
    with pytest.raises(PermissionError,match='frozen'):
        library.save('alice',frozen,'happy','happy',context,lambda:True)


def test_candidate_identity_stable_until_file_replaced(library,context):
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    first,=library.candidates('alice')
    same,=library.candidates('alice')
    assert same.id == first.id
    first.path.write_bytes(picture(color='green'))
    replacement,=library.candidates('alice')
    assert replacement.id != first.id


def test_owner_edit_during_preparation_is_not_overwritten(library,context,monkeypatch):
    item=upload(library)
    library.save('alice',item,'happy','first',context,lambda:True)
    second=upload(library,color='green')
    original=library._items
    manifest=library.library.root('alice')/'manifest.json'
    changed=None
    def edit(role_id,**kwargs):
        nonlocal changed
        result=original(role_id,**kwargs)
        changed=json.loads(manifest.read_text())
        changed['categories']['happy']['desc']='concurrent human edit'
        manifest.write_text(json.dumps(changed))
        return result
    monkeypatch.setattr(library,'_items',edit)
    with pytest.raises(PermissionError,match='conflict'):
        library.save('alice',second,'happy','new image',context,lambda:True)
    assert json.loads(manifest.read_text()) == changed
    assert len(list((manifest.parent/'happy').glob('*.png'))) == 1


def test_invalid_image_symlink_and_foreign_source_excluded(library,context,tmp_path):
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    other=tmp_path/'foreign.png'
    other.write_bytes(picture())
    candidate.path.unlink()
    candidate.path.symlink_to(other)
    assert not library.valid('alice',candidate)
    with pytest.raises(Exception):
        library.save('bob',item,'happy','foreign',context,lambda:True)


def test_real_managed_library_shares_revision_and_storage_without_drift_authority(tmp_path,context):
    import sqlite3
    from backend.app.proactive_runtime.meme_library import MemeLibrary
    from backend.app.proactive_runtime.drift_workspace import FileLimits
    db=sqlite3.connect(tmp_path/'isolated.db')
    db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE autonomy_roles (role_id TEXT PRIMARY KEY)')
    db.execute("INSERT INTO autonomy_roles VALUES ('alice')")
    db.commit()
    @contextmanager
    def transaction():
        with db:
            yield db
    root=tmp_path/'alice'/'workspace'
    root.mkdir(parents=True)
    grant=SimpleNamespace(enabled=True,managedEpoch='adopted-owner-epoch')
    app=SimpleNamespace(_transaction=transaction,_gate=threading.RLock(),
        drift_skills=SimpleNamespace(workspace=lambda role:root,configuration=lambda role:SimpleNamespace(driftMeme=grant)),
        drift_files=SimpleNamespace(limits=FileLimits()),role_getter=lambda role:object())
    shared=MemeLibrary(app)
    with transaction() as connection:
        shared.adopt(connection,'alice',grant)
    library=ChatMemeLibrary(shared,lambda role:root)
    item=upload(library)
    assert library.save('alice',item,'happy','ordinary owner save',context,lambda:True)['status']=='recorded'
    assert db.execute('SELECT revision FROM autonomy_meme_roots').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 0
    assert db.execute('SELECT committed_bytes FROM autonomy_meme_allocations').fetchone()[0] > len(picture())
    assert db.execute("SELECT COUNT(*) FROM autonomy_meme_operations WHERE json_extract(planned,'$.principal')='owner'").fetchone()[0] == 1
    # The durable operation is an owner fact, not an autonomous run or grant.
    assert library.reference('alice',library.candidates('alice')[0],context)[1] == picture()
    db.close()


def test_failed_reference_publication_can_retry_without_partial_file(library,context,monkeypatch):
    from backend.app.channels import meme_library as module
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    original=module.os.replace
    failed=False
    def interrupt(source,target):
        nonlocal failed
        if Path(target).suffix=='.json' and not failed:
            failed=True
            raise OSError('simulated local interruption')
        return original(source,target)
    monkeypatch.setattr(module.os,'replace',interrupt)
    with pytest.raises(OSError):
        library.reference('alice',candidate,context)
    assert not list((library.attachment_root('alice')/'attachments').glob('.chat-meme-*.tmp'))
    metadata,raw=library.reference('alice',candidate,context)
    assert Path(metadata['path']).read_bytes()==raw==picture()


def test_concurrent_owner_deletion_is_not_resurrected(library,context,monkeypatch):
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    original=library._source
    calls=0
    def delete(role_id,item):
        nonlocal calls
        calls+=1
        value=original(role_id,item)
        if calls==2:
            candidate.path.unlink()
        return value
    monkeypatch.setattr(library,'_source',delete)
    with pytest.raises(PermissionError,match='replaced'):
        library.save('alice',item,'happy','happy',context,lambda:True)
    assert not candidate.path.exists()


def test_bad_file_does_not_hide_genuine_candidates(library,context,tmp_path):
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    other=tmp_path/'foreign.png'
    other.write_bytes(picture())
    (candidate.path.parent/'bad.png').symlink_to(other)
    assert library.candidates('alice')[0].id==candidate.id
    assert library.candidates('alice','completely unrelated query')==()


def test_simultaneous_saves_in_different_existing_categories_never_duplicate(library,context,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    root=library.library.root('alice')
    root.mkdir(parents=True)
    (root/'manifest.json').write_text(json.dumps({'version':1,'categories':{
        category:{'desc':category,'aliases':[],'enabled':True} for category in ('happy','joy')}}))
    item=upload(library)
    barrier=threading.Barrier(2)
    lock=threading.Lock()
    scans=0
    original=library._items
    def synchronized_scan(*args,**kwargs):
        nonlocal scans
        with lock:
            scans+=1
            count=scans
        result=list(original(*args,**kwargs))
        if count<=2:
            barrier.wait(timeout=3)
        return iter(result)
    monkeypatch.setattr(library,'_items',synchronized_scan)
    def save(category):
        try:
            return library.save('alice',item,category,'same source',context,lambda:True)
        except PermissionError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(save,('happy','joy')))
    assert sum(isinstance(result,dict) for result in outcomes)==1
    assert sum(isinstance(result,PermissionError) for result in outcomes)==1
    assert len(list(root.glob('*/*.png')))==1
    retry=library.save('alice',item,'joy','same source',context,lambda:True)
    assert retry['deduplicated']
    assert len(list(root.glob('*/*.png')))==1


@pytest.mark.parametrize('managed',[False,True])
@pytest.mark.parametrize('phase',['before_publication','partial_publication','later_owner_replacement'])
def test_abrupt_copy_exit_reconciles_owned_stages_without_replaying(tmp_path,managed,phase):
    import subprocess
    import sys
    library=real_library(tmp_path,managed)
    item=upload(library)
    script=r'''
import os,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path.cwd()/'tests/python'))
from test_chat_meme_library import real_library
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.chat_attachments import resolve_attachment
from backend.app.channels import meme_library as module
library=real_library(Path(sys.argv[1]),sys.argv[2]=='True')
item=resolve_attachment(library.attachment_root('alice'),sys.argv[3])
context=SimpleNamespace(role_id='alice',session_key='thread:group',run_id='crashed-owner',tool_call_id='copy',signal=CancellationToken())
if sys.argv[4]=='before_publication':
    module.os.fsync=lambda descriptor: os._exit(73)
else:
    original=module.os.replace
    def interrupt(source,target):
        original(source,target)
        os._exit(73)
    module.os.replace=interrupt
library.save('alice',item,'happy','owner original',context,lambda:True)
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path),str(managed),item['id'],phase],check=False)
    assert result.returncode==73
    root=library.library.root('alice')
    finals=list(root.glob('*/*.png'))
    with library.library.app._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM autonomy_meme_operations WHERE status NOT IN ('recorded','reconciled')").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0]==int(managed)
    if phase=='later_owner_replacement':
        assert len(finals)==1
        finals[0].write_bytes(picture(color='green'))
    before={path:path.read_bytes() for path in finals}
    # A fresh library instance models restart; Drift-disabled owners also recover.
    restarted=real_library(tmp_path,managed)
    restarted.library.reconcile('alice')
    with restarted.library.app._transaction() as db:
        row=db.execute('SELECT status,actual FROM autonomy_meme_operations').fetchone()
        assert row['status']=='reconciled'
        actual=json.loads(row['actual'])
        assert actual['outcome']=={'before_publication':'not_published','partial_publication':'partial','later_owner_replacement':'unconfirmed'}[phase]
        assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0]==0
        if managed and finals:
            assert db.execute('SELECT committed_bytes FROM autonomy_meme_allocations').fetchone()[0]>=sum(len(data) for data in before.values())
    assert {path:path.read_bytes() for path in finals}==before
    assert not list((root.parent/'.meido-memes/staging').glob('*.tmp'))
    assert not (root/'manifest.json').exists()  # recovery did not complete the save


@pytest.mark.parametrize('managed',[False,True])
def test_abrupt_reference_exit_does_not_leave_unowned_attachment_stages(tmp_path,managed,context):
    import subprocess
    import sys
    library=real_library(tmp_path,managed)
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    script=r'''
import os,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path.cwd()/'tests/python'))
from test_chat_meme_library import real_library
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.channels import meme_library as module
library=real_library(Path(sys.argv[1]),sys.argv[2]=='True')
context=SimpleNamespace(role_id='alice',session_key='thread:group',run_id='crashed-reference',tool_call_id='reference',signal=CancellationToken())
module.os.fsync=lambda descriptor: os._exit(74)
library.reference('alice',library.candidates('alice')[0],context)
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path),str(managed)],check=False)
    assert result.returncode==74
    before={path:path.read_bytes() for path in (library.attachment_root('alice')/'attachments').iterdir()}
    restarted=real_library(tmp_path,managed)
    restarted.library.reconcile('alice')
    assert {path:path.read_bytes() for path in before}==before
    assert not list((library.library.root('alice').parent/'.meido-memes/staging').glob('*.tmp'))
    with restarted.library.app._transaction() as db:
        row=db.execute("SELECT status,actual FROM autonomy_meme_operations WHERE run_id='crashed-reference'").fetchone()
        assert row['status']=='reconciled'
        assert json.loads(row['actual'])['outcome']=='not_published'
    metadata,raw=restarted.reference('alice',restarted.candidates('alice')[0],context)
    assert Path(metadata['path']).read_bytes()==raw==picture()


def test_concurrent_equivalent_references_cleanup_stages_before_terminal_record(library,context,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from backend.app.channels.meme_library import _OwnerPublication
    item=upload(library)
    library.save('alice',item,'happy','happy',context,lambda:True)
    candidate,=library.candidates('alice')
    barrier=threading.Barrier(2)
    original_prepare,original_finish=_OwnerPublication.prepare,_OwnerPublication.finish
    def prepare(self,check):
        original_prepare(self,check)
        barrier.wait(timeout=3)
    checked=[]
    def finish(self,db):
        original_finish(self,db)
        # At this point close() has not run, but the terminal record can commit.
        assert all(not Path(entry['stage']).exists() for entry in self.plan['files'])
        checked.append(self.operation)
    monkeypatch.setattr(_OwnerPublication,'prepare',prepare)
    monkeypatch.setattr(_OwnerPublication,'finish',finish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:library.reference('alice',candidate,context),range(2)))
    assert len(checked)==2
    assert results[0][0]['id']==results[1][0]['id']
    assert results[0][1]==results[1][1]==picture()
    assert not list((library.library.root('alice').parent/'.meido-memes/staging').glob('*.tmp'))
