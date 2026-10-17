"""Two distinct real application processes consume the same owned fixture library."""
import asyncio
import base64
import io
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import sysconfig
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(sys.argv[1]).absolute()
PHASE = int(sys.argv[2])
assert PHASE in {1, 2}
dependency_paths_json = os.environ.get('MEIDO_TEST_SITE_PATHS')
getattr(sys.stdout, 'reconfigure')(encoding='utf-8')
getattr(sys.stderr, 'reconfigure')(encoding='utf-8')
HOME = DATA / ('home-' + str(PHASE))
TEMP = DATA / ('tmp-' + str(PHASE))
assert not HOME.exists() and not TEMP.exists()
HOME.mkdir(parents=True)
TEMP.mkdir()
os.environ.clear()
os.environ.update({'SYSTEMROOT': 'C:\\Windows', 'WINDIR': 'C:\\Windows', 'PATH': 'C:\\Windows\\System32;C:\\Windows',
    'HOME': str(HOME), 'USERPROFILE': str(HOME), 'TMP': str(TEMP), 'TEMP': str(TEMP),
    'APPDATA': str(HOME / 'AppData/Roaming'), 'LOCALAPPDATA': str(HOME / 'AppData/Local'),
    'MEIDO_DATA_DIR': str(DATA / 'data'), 'MEIDO_ROLES_DIR': str(DATA / 'roles'),
    'MEIDO_OWNER_KNOWLEDGE_DATA_DIR': str(DATA / 'owner'), 'MEIDO_BROWSER_USE_DATA_DIR': str(DATA / 'browser'),
    'MEIDO_PROJECT_ROOT': str(DATA / 'project'), 'MEIDO_MEMORY_OPTIMIZER_ENABLED': '0',
    'MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED': '0', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1'})
for key in ('MEIDO_DATA_DIR', 'MEIDO_ROLES_DIR', 'MEIDO_OWNER_KNOWLEDGE_DATA_DIR', 'MEIDO_BROWSER_USE_DATA_DIR', 'MEIDO_PROJECT_ROOT'):
    Path(os.environ[key]).mkdir(exist_ok=True)
(DATA / 'data/memory-settings.json').write_text('{"enabled":false,"embeddingEnabled":false}', encoding='utf-8')
sys.dont_write_bytecode = True
dependency_paths = (json.loads(dependency_paths_json) if dependency_paths_json else
    [sysconfig.get_path('purelib'), sysconfig.get_path('platlib')])
assert isinstance(dependency_paths, list) and all(isinstance(path, str) and Path(path).is_absolute() for path in dependency_paths)
sys.path[:0] = [str(ROOT), *dict.fromkeys(dependency_paths)]
initial_threads = {id(thread) for thread in threading.enumerate()}
blocked: list[str] = []
def check(host):
    if host is not None and host not in {'localhost', '127.0.0.1', '::1', b'localhost', b'127.0.0.1', b'::1'}:
        blocked.append(str(host))
        raise RuntimeError('Drift191 child forbids non-local network')
resolve, connect, connect_ex, create = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, asyncio.BaseEventLoop.create_connection
def guarded_resolve(host, *args, **kwargs):
    check(host)
    return resolve(host, *args, **kwargs)
def guarded_connect(sock, address):
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        check(address[0])
    return connect(sock, address)
def guarded_connect_ex(sock, address):
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        check(address[0])
    return connect_ex(sock, address)
async def guarded_create(loop, factory, host=None, port=None, **kwargs):
    check(host)
    return await create(loop, factory, host=host, port=port, **kwargs)
socket.getaddrinfo = guarded_resolve
setattr(socket.socket, 'connect', guarded_connect)
setattr(socket.socket, 'connect_ex', guarded_connect_ex)
setattr(asyncio.BaseEventLoop, 'create_connection', guarded_create)
from backend.app.credentials import SystemCredentialStore
vault = {'get': 0, 'set': 0}
def empty(store, key):
    vault['get'] += 1
    return ''
def deny(store, key, value):
    vault['set'] += 1
    raise RuntimeError('Drift191 child forbids vault writes')
SystemCredentialStore.get, SystemCredentialStore.set = empty, deny
from fastapi.testclient import TestClient
from PIL import Image
from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelImageBlock, ModelStreamDone, ModelToolCallBlock, ModelTextBlock

proof: dict[str, Any] = {'phase': PHASE, 'pid': os.getpid(), 'memory': False, 'channels': False, 'errors': []}
for index, callback in enumerate(main.app.router.on_shutdown):
    async def close(callback=callback):
        try:
            await callback()
            if callback is main._close_memory_workers:
                proof['memory'] = True
            if callback is main._close_channel_workers:
                proof['channels'] = True
        except BaseException as error:
            proof['errors'].append(type(error).__name__)
            raise
    main.app.router.on_shutdown[index] = close


def finish_pending_child():
    for thread in threading.enumerate():
        if id(thread) not in initial_threads and thread is not threading.current_thread():
            thread.join()
    proof.update({'blockedNonLocal': blocked, 'vault': vault,
        'newThreads': [thread.name for thread in threading.enumerate() if id(thread) not in initial_threads]})
    complete = proof['memory'] and proof['channels'] and not proof['errors'] and not blocked and not proof['newThreads'] and vault['set'] == 0
    if complete:
        socket.getaddrinfo = resolve
        setattr(socket.socket, 'connect', connect)
        setattr(socket.socket, 'connect_ex', connect_ex)
        setattr(asyncio.BaseEventLoop, 'create_connection', create)
    proof['guardRestoredAfterOriginalOwners'] = complete
    (DATA / ('phase-' + str(PHASE) + '-result.json')).write_text(json.dumps(proof, ensure_ascii=False), encoding='utf-8')
    assert complete, 'The original application, runtime and fixture threads must really finish'
    print(json.dumps(proof, ensure_ascii=False))


previous: dict[str, Any]
adapter: Any
if len(sys.argv) > 3 and sys.argv[3] == 'pending-disabled':
    identity_path = DATA / 'pending-fixture-identity.json'
    previous = json.loads(identity_path.read_bytes()) if PHASE == 2 else {}
    now = [previous['clock'] + 10800 if previous else time.time()]
    main._proactive_clock = lambda: now[0]
    class PendingMaintenance:
        requests = 0
        image_requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if PHASE == 1:
                sources = next(json.loads(block.text.removeprefix('本run获准表情素材：')) for message in messages for block in message.content
                    if isinstance(block, ModelTextBlock) and block.text.startswith('本run获准表情素材：'))
                assert len(sources) == 1
                steps = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/manifest.json'}),
                    ('meme_copy_image', {'attachment_id': sources[0]['id'], 'category': 'happy'})]
                name, arguments = steps[self.requests - 1]
                block = ModelToolCallBlock('pending-' + str(self.requests), name, arguments)
            else:
                images = [block for message in messages for block in message.content if isinstance(block, ModelImageBlock)]
                if images:
                    self.image_requests += 1
                    assert self.requests == 3 and len(images) == 1
                    with Image.open(io.BytesIO(base64.b64decode(images[0].data_base64))) as opened:
                        assert opened.size == (800, 600) and opened.getpixel((5, 5)) == (93, 74, 161)
                steps = [ModelToolCallBlock('owner-new-manifest', 'write_file', {'path': 'memes/manifest.json',
                    'content': json.dumps({'version': 1, 'categories': {'happy': {'desc': '重建后主人真实采用', 'aliases': ['保留'], 'enabled': True}}}, ensure_ascii=False)}),
                    ModelToolCallBlock('owner-read-copy', 'read_file', {'path': previous['imagePath']}),
                    ModelTextBlock('主人已实际读取同一新复制产物')]
                assert self.requests <= len(steps), 'Recovery must not replay the previous model or copy'
                block = steps[self.requests - 1]
            yield ModelStreamDone(ModelAssistantMessage((block,)))
    adapter = PendingMaintenance()
    main.model_adapter = adapter
    try:
        with TestClient(main.app) as client:
            if PHASE == 1:
                role = client.post('/api/roles', json={'name': '完整重建未决副本191', 'profile': {'profile': '本地虚构素材'},
                    'agentConfig': {'enabledTools': ['read_file', 'list_dir', 'write_file', 'edit_file']}}).json()['role']['id']
                model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                    'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
                image = io.BytesIO()
                Image.new('RGB', (800, 600), '#5d4aa1').save(image, 'PNG')
                source = client.post(f'/api/roles/{role}/attachments', content=image.getvalue(),
                    headers={'x-attachment-filename': 'owned-copy.png', 'content-type': 'image/png'}).json()
                path = f'/api/roles/{role}/autonomy'
                response = client.put(path, json={'expectedRevision': 0, 'config': {
                    'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
                    'driftToolGrants': ['read_file', 'list_dir', 'write_file', 'edit_file'],
                    'driftMeme': {'enabled': True, 'visionModelConfigurationId': model['id'], 'readEnabled': True,
                        'editEnabled': True, 'copyEnabled': True, 'allowedCategories': ['happy'], 'sourceAttachmentIds': [source['id']]}}})
                assert response.status_code == 200, response.text
                library = main.store.workspace_path(role) / 'memes'
                (library / 'manifest.json').write_text('{"version":1,"categories":{"happy":{"desc":"original","aliases":[],"enabled":true}}}', encoding='utf-8')
                actual_replace = os.replace
                def lost_image_response(stage, target):
                    result = actual_replace(stage, target)
                    if Path(target).parent == library / 'happy':
                        raise OSError('fixture: original caller lost the completed image rename response')
                    return result
                setattr(os, 'replace', lost_image_response)
                try:
                    now[0] += 1800
                    async def attempt():
                        await main._proactive.on_clock()
                        async with asyncio.timeout(20):
                            while True:
                                await main._proactive.recover()
                                state = main._proactive.query(role)
                                if state['runs'] and all(item['runtimeAcknowledged'] for item in state['runs']):
                                    return state
                                await asyncio.sleep(.01)
                    state = client.portal.call(attempt)
                finally:
                    os.replace = actual_replace
                assert state['runs'][0]['status'] == 'aborted' and adapter.requests == 3, {
                    'modelRequests': adapter.requests, 'runs': [{'status': item['status'], 'error': item['error']} for item in state['runs']]}
                operation = client.get(path + '/meme-library').json()['operations'][0]
                assert operation['status'] == 'cleanup_pending' and operation['physicalPhase'] == 'files_published'
                config = client.get(path).json()['config']
                assert client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
                    'driftMeme': {**config['driftMeme'], 'enabled': False}}}).status_code == 200
                relative = 'memes/' + operation['path'].removeprefix('@memes/')
                copied = main.store.workspace_path(role) / relative
                assert copied.read_bytes() == image.getvalue()
                identity_path.write_text(json.dumps({'roleId': role, 'clock': now[0], 'imagePath': relative,
                    'imageSha256': hashlib.sha256(image.getvalue()).hexdigest(), 'operationId': operation['operationId']}), encoding='utf-8')
                proof.update({'roleId': role, 'originalStatus': operation['status'], 'modelRequests': adapter.requests,
                    'copiedSha256': hashlib.sha256(copied.read_bytes()).hexdigest()})
            else:
                role = previous['roleId']
                path = f'/api/roles/{role}/autonomy'
                assert client.get(path).json()['config']['driftMeme']['enabled'] is False
                response = client.post(f'/api/roles/{role}/messages', json={'content': '关闭用途重建后，主人直接编辑并实际读同一新副本'})
                assert 'assistant_completed' in response.text, response.text[-1000:]
                client.portal.call(main._proactive.recover)
                operation = client.get(path + '/meme-library').json()['operations'][0]
                assert operation['operationId'] == previous['operationId'] and operation['status'] == 'conflict'
                assert operation['physicalPhase'] == 'files_published'
                workspace = main.store.workspace_path(role)
                assert json.loads((workspace / 'memes/manifest.json').read_text(encoding='utf-8'))['categories']['happy']['desc'] == '重建后主人真实采用'
                copied = workspace / previous['imagePath']
                assert hashlib.sha256(copied.read_bytes()).hexdigest() == previous['imageSha256']
                assert len(list(copied.parent.iterdir())) == 1 and adapter.requests == 3 and adapter.image_requests == 1
                proof.update({'roleId': role, 'recoveredStatus': operation['status'], 'modelRequests': adapter.requests,
                    'actualImageRequests': adapter.image_requests, 'copiedSha256': hashlib.sha256(copied.read_bytes()).hexdigest()})
    except BaseException as error:
        proof['bodyErrorType'] = type(error).__name__
        raise
    finally:
        finish_pending_child()
    raise SystemExit(0)

identity_path = DATA / 'fixture-identity.json'
previous = json.loads(identity_path.read_bytes()) if PHASE == 2 else {}
now = [previous['clock'] + 10800 if previous else time.time()]
main._proactive_clock = lambda: now[0]
class Maintenance:
    requests = 0
    image_requests = 0
    owner_requests = 0
    owner_image_requests = 0
    copied_path = ''
    operation_id = ''
    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        images = [block for message in messages for block in message.content if isinstance(block, ModelImageBlock)]
        if any('主人实际读取重建后同一新复制产物' in getattr(block, 'text', '') for message in messages for block in message.content):
            self.owner_requests += 1
            if self.owner_requests == 1:
                block = ModelToolCallBlock('owner-real-new-image', 'read_file', {'path': previous['imagePath']})
            else:
                assert self.owner_requests == 2 and len(images) == 1
                with Image.open(io.BytesIO(base64.b64decode(images[0].data_base64))) as actual:
                    assert actual.size == (800, 600) and actual.getpixel((5, 5)) == (93, 74, 161)
                self.owner_image_requests += 1
                block = ModelTextBlock('主人也已实际查看同一新复制产物')
            yield ModelStreamDone(ModelAssistantMessage((block,)))
            return
        self.requests += 1
        if self.requests == 1 and PHASE == 2:
            assert any('跨进程继续采用' in getattr(block, 'text', '') for message in messages for block in message.content)
        if self.requests == 4 and PHASE == 1:
            receipt = json.loads(messages[-1].content[0].text)
            assert receipt['status'] == 'recorded' and receipt['doesNotCreateSendingIdentity'] is True
            self.copied_path, self.operation_id = receipt['path'], receipt['operationId']
        if images:
            self.image_requests += 1
            assert self.requests == (6 if PHASE == 1 else 4) and len(images) == 1
            with Image.open(io.BytesIO(base64.b64decode(images[0].data_base64))) as actual:
                assert actual.size == (800, 600) and actual.getpixel((5, 5)) == (93, 74, 161)
            assert any(('第一轮真实类别' if PHASE == 2 else '主人初始类别') in getattr(block, 'text', '') for message in messages for block in message.content)
        desired = {'version': 1, 'categories': {'happy': {'desc': '第一轮真实类别' if PHASE == 1 else '第二进程继续维护',
            'aliases': ['rebuild-' + str(PHASE)], 'enabled': True}}}
        steps = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/manifest.json'})]
        if PHASE == 1:
            source_block = next(block.text for message in messages for block in message.content
                if isinstance(block, ModelTextBlock) and block.text.startswith('本run获准表情素材：'))
            sources = json.loads(source_block.removeprefix('本run获准表情素材：'))
            assert len(sources) == 1
            steps.extend([('meme_copy_image', {'attachment_id': sources[0]['id'], 'category': 'happy'}),
                ('read_file', {'path': '@memes/manifest.json'}),
                ('read_file', {'path': self.copied_path})])
        else:
            steps.append(('read_file', {'path': '@memes/' + previous['imagePath'].removeprefix('memes/')}))
        steps.extend([
            ('write_file', {'path': '@memes/manifest.json', 'content': json.dumps(desired, ensure_ascii=False)}),
            ('finish_drift', {'skill_used': 'meme-manage', 'one_line': '实际进程' + str(PHASE) + '维护',
                'next_action': '跨进程继续采用', 'message_result': 'silent'})])
        assert self.requests <= len(steps)
        name, arguments = steps[self.requests - 1]
        yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('process-' + str(PHASE) + '-' + str(self.requests), name, arguments),)))
adapter = Maintenance()
main.model_adapter = adapter
try:
    with TestClient(main.app) as client:
        if PHASE == 1:
            role = client.post('/api/roles', json={'name': '真实复制与进程重建191', 'profile': {'profile': '本地虚构素材'},
                'agentConfig': {'enabledTools': ['read_file', 'list_dir', 'write_file', 'edit_file']}}).json()['role']['id']
            model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
            image = io.BytesIO()
            Image.new('RGB', (800, 600), '#5d4aa1').save(image, 'PNG')
            uploaded = client.post(f'/api/roles/{role}/attachments', content=image.getvalue(),
                headers={'x-attachment-filename': 'new-source.png', 'content-type': 'image/png'})
            assert uploaded.status_code == 200, uploaded.text
            source = uploaded.json()
            response = client.put(f'/api/roles/{role}/autonomy', json={'expectedRevision': 0, 'config': {
                'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
                'driftToolGrants': ['read_file', 'list_dir', 'write_file', 'edit_file'],
                'driftMeme': {'enabled': True, 'visionModelConfigurationId': model['id'], 'readEnabled': True,
                    'editEnabled': True, 'copyEnabled': True, 'allowedCategories': ['happy'], 'sourceAttachmentIds': [source['id']]}}})
            assert response.status_code == 200, response.text
            library = main.store.workspace_path(role) / 'memes'
            (library / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {'happy': {
                'desc': '主人初始类别', 'aliases': [], 'enabled': True}}}, ensure_ascii=False), encoding='utf-8')
        else:
            role = previous['roleId']
            assert main.store.get(role) is not None
        now[0] += 1800
        async def run():
            await main._proactive.on_clock()
            async with asyncio.timeout(20):
                while True:
                    await main._proactive.recover()
                    state = main._proactive.query(role)
                    if state['runs'] and all(item['runtimeAcknowledged'] for item in state['runs']):
                        return state
                    await asyncio.sleep(.01)
        state = client.portal.call(run)
        assert state['runs'][0]['status'] == 'finished_silent', state
        assert adapter.requests == (7 if PHASE == 1 else 5) and adapter.image_requests == 1
        path = f'/api/roles/{role}/autonomy'
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'meme-manage')
        assert skill['progress']['runCount'] == PHASE and skill['progress']['nextAction'] == '跨进程继续采用'
        if PHASE == 1:
            relative = 'memes/' + adapter.copied_path.removeprefix('@memes/')
            copied = main.store.workspace_path(role) / relative
            assert copied.read_bytes() == image.getvalue()
            metadata = json.loads((copied.parent.parent.parent / '.meido-memes/metadata' / (adapter.operation_id + '.json')).read_bytes())
            assert metadata['sourcePublicId'] == source['id']
            assert json.loads((copied.parent.parent / 'manifest.json').read_bytes())['categories']['happy']['desc'] == '第一轮真实类别'
            previous = {'roleId': role, 'clock': now[0], 'imagePath': relative,
                'imageSha256': hashlib.sha256(copied.read_bytes()).hexdigest(), 'operationId': adapter.operation_id}
            identity_path.write_text(json.dumps(previous), encoding='utf-8')
        else:
            response = client.post(f'/api/roles/{role}/messages', json={'content': '主人实际读取重建后同一新复制产物'})
            assert 'assistant_completed' in response.text, response.text[-1000:]
            assert adapter.owner_requests == 2 and adapter.owner_image_requests == 1
            copied = main.store.workspace_path(role) / previous['imagePath']
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == previous['imageSha256']
            assert json.loads((copied.parent.parent / 'manifest.json').read_bytes())['categories']['happy']['desc'] == '第二进程继续维护'
        proof.update({'roleId': role, 'progress': skill['progress'], 'actualImageRequests': adapter.image_requests,
            'ownerImageRequests': adapter.owner_image_requests, 'clock': now[0], 'imagePath': previous['imagePath'],
            'copiedSha256': hashlib.sha256(copied.read_bytes()).hexdigest()})
except BaseException as error:
    proof['bodyErrorType'] = type(error).__name__
    raise
finally:
    finish_pending_child()
