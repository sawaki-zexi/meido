"""Two real backend processes restore the same approved native desktop companion."""
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
    'MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED': '0', 'MEIDO_PET_NATIVE_TEST': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1'})
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
        raise RuntimeError('Pet195 child forbids non-local network')
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
    raise RuntimeError('Pet195 child forbids vault writes')
SystemCredentialStore.get, SystemCredentialStore.set = empty, deny
from fastapi.testclient import TestClient
from PIL import Image
from backend.app import main
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


from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.agent_runtime.types import ToolResultMessage
import zipfile


identity = DATA / 'pet-identity.json'
role = None
host = None
try:
    with TestClient(main.app) as client:
        if PHASE == 1:
            image = Image.new('RGBA', (1536, 1872))
            for row, count in enumerate((6, 8, 8, 4, 5, 8, 6, 6, 6)):
                for column in range(count):
                    image.paste((80, 130, 170, 255), (column*192+32, row*208+32, column*192+160, row*208+176))
            buffer = io.BytesIO()
            image.save(buffer, 'WEBP', lossless=True)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('pet.json', json.dumps({'id': 'rebuilt-pet', 'displayName': '重建桌宠',
                    'description': '自制素材', 'spritesheetPath': 'sprite.webp', 'actions': {'wave': 'waving'}}))
                bundle.writestr('sprite.webp', buffer.getvalue())
            role = client.post('/api/roles', json={'name': '重建角色', 'profile': {'profile': '本地'},
                'agentConfig': {'enabledTools': ['tool_search']}}).json()['role']['id']
            path = f'/api/roles/{role}/desktop-pet'
            state = client.post(path + '/packages', content=archive.getvalue()).json()
            state = client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'rebuilt-pet', 'enabled': True}).json()
            assert state['host']['status'] == 'ready', state
            identity.write_text(json.dumps({'roleId': role}), encoding='utf-8')
        else:
            role = json.loads(identity.read_text(encoding='utf-8'))['roleId']
            path = f'/api/roles/{role}/desktop-pet'
            state = client.get(path).json()
            assert state['enabled'] and state['host']['status'] == 'ready', state
        assert state['host']['window']['alwaysOnTop'] and state['host']['window']['visible'], state
        host = main._desktop_pet_application().host
        class Adapter:
            called = False
            async def stream_structured_messages(self, messages, configuration, tools):
                available = {item['function']['name'] for item in tools}
                if 'pet_action' not in available:
                    yield ModelToolCall('find-pet', 'tool_search', {'query': 'select:pet_action'})
                elif not self.called:
                    self.called = True
                    yield ModelToolCall('wave-pet', 'pet_action', {'action': 'play', 'name': 'wave'})
                else:
                    yield ModelTextDelta('重建后实际完成')
        main.model_adapter = Adapter()
        response = client.post(f'/api/roles/{role}/messages', json={'content': '挥手'})
        assert 'assistant_completed' in response.text, response.text
        result = [item for item in main.session_store.runtime_messages(f'role:{role}')
            if isinstance(item, ToolResultMessage) and item.tool_name == 'pet_action'][-1]
        assert not result.is_error and result.details['type'] == 'finished' and result.details['frames'] == 12, result
        if PHASE == 2:
            capture = host.root / 'own-window.png'
            with Image.open(capture) as rendered:
                assert rendered.getpixel((rendered.width // 2, rendered.height // 2))[:3] == (80, 130, 170)
                proof['ownWindowCapture'] = {'path': str(capture), 'size': rendered.size, 'nativePid': host.process.pid}
        proof.update({'roleId': role, 'nativePid': host.process.pid, 'actionId': result.details['actionId'],
            'receipt': result.details, 'window': state['host']['window']})
    assert host.closed and host.job.exit_confirmed and all(not thread.is_alive() for thread in host._threads)
    proof['nativeTreeExited'] = True
finally:
    finish_pending_child()
