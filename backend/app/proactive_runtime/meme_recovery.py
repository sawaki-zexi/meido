"""Inspect original owned facts; recovery never publishes a planned library edit."""
import hashlib
import json
from pathlib import Path

from .drift_workspace import _identity
from .source_authoring import _directory_identity
from .source_validation import _read
from .meme_library import _json


def _published(path, stage, digest, actual):
    if not path.exists():
        return None
    identity = list(_identity(path))
    if identity[:2] != actual.get('staging', {}).get(str(stage)):
        raise PermissionError('a later file occupies the planned final path')
    if hashlib.sha256(_read(path, 10 * 1024 * 1024)).hexdigest() != digest:
        raise PermissionError('the original published file was subsequently changed')
    return {'identity': identity, 'sha256': digest}


def _cleanup(private, actual):
    # The row is not authority to remove a same-name file in a replaced directory.
    for name, identity in actual.get('privateParents', []):
        path = Path(name)
        if not path.is_relative_to(private) or list(_directory_identity(path)) != identity:
            raise PermissionError('private staging directory identity changed')
    for name, identity in actual.get('staging', {}).items():
        path = Path(name)
        if not path.is_relative_to(private / 'staging'):
            raise PermissionError('staging journal escaped its original private root')
        if path.exists():
            if not actual.get('privateParents') or list(_identity(path)[:2]) != identity:
                raise PermissionError('staging file no longer belongs to the original operation')
            path.unlink()


def reconcile_owner(library, row):
    """Retire ordinary copy responsibility; never complete or retry its intent.

    Only owned private stages may be removed. Final files, including replacements
    made by the owner, are observations and are never modified by recovery.
    """
    role_id, root = row['role_id'], library.root(row['role_id'])
    expected, planned, actual = (json.loads(row[key]) for key in ('expected','planned','actual'))
    status = 'cleanup_pending'
    try:
        if library._root_identity(role_id) != expected['root']:
            raise PermissionError('ordinary meme original root changed')
        published, unconfirmed = [], False
        for item in planned['files']:
            target, stage = Path(item['target']), Path(item['stage'])
            allowed = (root, root.parent/'.meido-memes/chat') if planned['kind']=='owner_chat_copy' else (root.parent/'attachments',)
            if not any(target.is_relative_to(parent) for parent in allowed):
                raise PermissionError('ordinary meme target escaped its fixed role scope')
            if not stage.is_relative_to(root.parent/'.meido-memes/staging'):
                raise PermissionError('ordinary meme stage escaped its private scope')
            if not target.exists():
                if str(stage) in actual.get('staging',{}) and not stage.exists():
                    unconfirmed=True
                continue
            try:
                identity=list(_identity(target))
                if identity[:2] == actual.get('staging',{}).get(str(stage)):
                    if hashlib.sha256(_read(target,10*1024*1024)).hexdigest()==item['sha256']:
                        published.append(str(target))
                    else:
                        unconfirmed=True
                elif identity != item['expectedIdentity']:
                    unconfirmed=True
            except (OSError,ValueError,PermissionError):
                unconfirmed=True
        _cleanup(root.parent/'.meido-memes',actual)
        # A partial copy must invalidate old managed observations, but changing
        # epoch already invalidated them. Never reconstruct an old permission.
        if planned['kind']=='owner_chat_copy' and (published or actual.get('published')):
            with library.app._transaction() as db:
                db.execute('UPDATE autonomy_meme_roots SET revision=revision+1 WHERE role_id=? AND epoch=? AND revision=?',
                    (role_id,row['epoch'],expected['libraryRevision']))
        actual.update(reconciled=True, observedPublished=published,
            outcome='unconfirmed' if unconfirmed else 'partial' if published or actual.get('published') else 'not_published')
        status='reconciled'
    except PermissionError:
        status='conflict'
    except (OSError,ValueError,KeyError,TypeError):
        status='cleanup_pending'
    with library.app._transaction() as db:
        db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
            (status,_json(actual),row['operation_id']))
    library.storage.reconciled(role_id,row['operation_id'],status)


def reconcile(library, role_id):
    app, root = library.app, library.root(role_id)
    with app._transaction() as db:
        rows = db.execute("SELECT * FROM autonomy_meme_operations WHERE role_id=? AND status NOT IN ('recorded','not_published','reconciled')", (role_id,)).fetchall()
    for row in rows:
        # UI publications have no model/session active_run. Their host owner is
        # registered before journaling and lives through cleanup/quota release.
        # Re-read after the guard: a previously selected row may have completed
        # before its owner unregistered. Never reconcile that stale projection.
        with library._recovery_gate_lock:
            if (role_id, row['run_id']) in getattr(library, '_owner_material_runs', ()):
                continue
            with app._transaction() as db:
                row = db.execute("SELECT * FROM autonomy_meme_operations WHERE operation_id=? AND status NOT IN ('recorded','not_published','reconciled')", (row['operation_id'],)).fetchone()
            if row is None:
                continue
        actual, expected, planned = json.loads(row['actual']), json.loads(row['expected']), json.loads(row['planned'])
        if planned.get('kind') == 'owner_ui_remove':
            from ..material_library import reconcile_removal
            reconcile_removal(library, row)
            continue
        if planned.get('kind') in {'owner_chat_copy','owner_chat_reference','natural_chat_reference','role_reaction_reference'}:
            reconcile_owner(library,row)
            continue
        phase, status = actual.get('physicalPhase', 'reserved'), 'cleanup_pending'
        try:
            if library._root_identity(role_id) != expected['root']:
                raise PermissionError('original library root changed')
            with app._transaction() as db:
                adopted = db.execute('SELECT epoch,revision FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
            if adopted is None or adopted[0] != row['epoch']:
                raise PermissionError('original managed library epoch changed')
            if planned['kind'] == 'copy':
                names = ('image', 'metadata', 'manifest')
                stages = tuple(Path(path) for path in planned['stages'])
                facts = {}
                for name, stage in zip(names[:2], stages[:2]):
                    path = Path(planned[name])
                    if not path.is_relative_to(root if name == 'image' else root.parent / '.meido-memes/metadata'):
                        raise PermissionError('original target escaped its fixed library root')
                    fact = _published(path, stage, planned[name + 'Sha256'], actual)
                    if fact is not None:
                        facts[name] = fact
                        phase = 'files_published'
                manifest = Path(planned['manifest'])
                if manifest != root / 'manifest.json':
                    raise PermissionError('original manifest is outside the fixed library')
                current = library._fact(role_id, manifest)
                own_manifest = (current.get('file', []) or [])[:2] == actual.get('staging', {}).get(str(stages[2]))
                if own_manifest:
                    facts['manifest'] = _published(manifest, stages[2], planned['manifestSha256'], actual)
                    phase = 'manifest_published'
                    if len(facts) != 3 or adopted[1] not in {expected['libraryRevision'], expected['libraryRevision'] + 1}:
                        raise PermissionError('published maintenance does not match all original planned facts')
                    if adopted[1] == expected['libraryRevision']:
                        with app._transaction() as db:
                            if db.execute('UPDATE autonomy_meme_roots SET revision=revision+1 WHERE role_id=? AND epoch=? AND revision=?',
                                    (role_id, row['epoch'], expected['libraryRevision'])).rowcount != 1:
                                raise PermissionError('library changed during factual recovery')
                    status = 'recorded'
                elif current != expected:
                    raise PermissionError('owner manifest or library revision changed after the original observation')
                elif not facts:
                    status = 'not_published'
                actual.update(facts)
            else:
                target, stage = Path(row['path']), Path(row['temporary'])
                if not target.is_relative_to(root):
                    raise PermissionError('original library write escaped its root')
                current = library._fact(role_id, target)
                owned = (current.get('file', []) or [])[:2] == actual.get('staging', {}).get(str(stage))
                if owned:
                    actual['file'] = _published(target, stage, row['digest'], actual)
                    phase = 'manifest_published' if target.name == 'manifest.json' else 'files_published'
                    if adopted[1] not in {expected['libraryRevision'], expected['libraryRevision'] + 1}:
                        raise PermissionError('library revision changed after publication')
                    if adopted[1] == expected['libraryRevision']:
                        with app._transaction() as db:
                            if db.execute('UPDATE autonomy_meme_roots SET revision=revision+1 WHERE role_id=? AND epoch=? AND revision=?',
                                    (role_id, row['epoch'], expected['libraryRevision'])).rowcount != 1:
                                raise PermissionError('library changed during factual recovery')
                    status = 'recorded'
                elif current != expected:
                    raise PermissionError('owner changed the original observed library file')
                else:
                    status = 'not_published'
            _cleanup(root.parent / '.meido-memes', actual)
        except PermissionError:
            status = 'conflict'
        except (OSError, ValueError, KeyError, TypeError):
            status = 'cleanup_pending'
        actual['physicalPhase'] = phase
        with app._transaction() as db:
            db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?', (status, _json(actual), row['operation_id']))
        library.storage.reconciled(role_id, row['operation_id'], status)
