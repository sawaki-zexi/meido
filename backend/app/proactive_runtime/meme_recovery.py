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


def reconcile(library, role_id):
    app, root = library.app, library.root(role_id)
    with app._transaction() as db:
        rows = db.execute("SELECT * FROM autonomy_meme_operations WHERE role_id=? AND status NOT IN ('recorded','not_published')", (role_id,)).fetchall()
    for row in rows:
        actual, expected, planned = json.loads(row['actual']), json.loads(row['expected']), json.loads(row['planned'])
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
