import pytest


@pytest.fixture(autouse=True)
def novelai_environment(tmp_path, monkeypatch, local_platform_only):
    """Keep imported application globals and every worker inside this test root."""
    monkeypatch.setenv('MEIDO_DATA_DIR', str(tmp_path / 'app-data'))
    monkeypatch.setenv('MEIDO_ROLES_DIR', str(tmp_path / 'roles'))
    monkeypatch.setenv('MEIDO_OWNER_KNOWLEDGE_DATA_DIR', str(tmp_path / 'owner-data'))
    monkeypatch.delenv('MEIDO_PROJECT_ROOT', raising=False)
    yield


@pytest.fixture
def novelai_service(tmp_path, novelai_environment):
    import asyncio
    import io
    import json
    import threading
    import zipfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace
    from PIL import Image
    from backend.app.novelai_generation.application import NovelaiApplication
    from backend.app.novelai_generation.http import NovelaiHttp
    from backend.app.models import RoleInput
    from backend.app.role_store import RoleStore
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.task_runtime import TaskRuntime
    from test_novelai_configuration import Credentials
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    roles, tasks = RoleStore(tmp_path / 'roles'), TaskRuntime(tmp_path / 'tasks')
    image = io.BytesIO()
    Image.new('RGB', (64, 64), '#171abc').save(image, format='PNG')
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        archive.writestr('image.png', image.getvalue())
    peer = SimpleNamespace(status=201, body=body.getvalue(), media_type='application/zip',
        requests=[], headers={}, accepted=threading.Event(), release=threading.Event(), block=False)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            peer.requests.append({'path': self.path, 'body': json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                                  'authorization': self.headers.get('Authorization')})
            peer.accepted.set()
            if peer.block:
                peer.release.wait(5)
            self.send_response(peer.status)
            self.send_header('Content-Type', peer.media_type)
            for key, value in peer.headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(peer.body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    app = NovelaiApplication(root=tmp_path / 'generation', tasks=tasks, sessions=sessions, roles=roles,
        channels=None, credentials=Credentials(), http=NovelaiHttp.for_test(f'http://127.0.0.1:{server.server_port}'))
    app.settings.set_token('fixture-secret-171')
    config = app.settings.save({'enabled': True}, expected_revision=app.settings.public()['revision'])
    role = roles.create(RoleInput(name='生图角色', profile={'profile': 'test character'},
        agentConfig={'novelai': {'manualEnabled': True, 'consentRevision': config['revision']}}))
    sessions.open_role_session(role.id)
    try:
        yield SimpleNamespace(application=app, roles=roles, role=role, sessions=sessions, tasks=tasks,
            peer=peer, request={'prompt': 'blue sky', 'mode': 'txt2img', 'seed': 171,
                'size_preset': 'custom', 'custom_width': 64, 'custom_height': 64})
    finally:
        peer.release.set()
        asyncio.run(app.close())
        tasks.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def novelai_main(novelai_service, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import main
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.channels.host import ChannelHost
    from backend.app.memory_config import MemorySettingsStore
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.multichannel import MultichannelStore
    from backend.app.session_manager import SessionManager
    from test_novelai_configuration import Credentials
    service = novelai_service
    channels = MultichannelStore(service.sessions.database_path, service.sessions, Credentials())
    memory = MemorySettingsStore(tmp_path / 'memory-settings.json')
    memory.update(enabled=False)
    monkeypatch.setattr(main, 'store', service.roles)
    monkeypatch.setattr(main, 'session_store', service.sessions)
    monkeypatch.setattr(main, 'session_manager', SessionManager(service.roles, service.sessions))
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(service.sessions))
    monkeypatch.setattr(main, 'role_locks', {})
    monkeypatch.setattr(main, 'model_configuration_store', ModelConfigurationStore(tmp_path / 'models.json'))
    monkeypatch.setattr(main, 'memory_settings_store', memory)
    monkeypatch.setattr(main, 'task_runtime', service.tasks)
    monkeypatch.setattr(main, 'multichannel_store', channels)
    monkeypatch.setattr(main, 'channel_host', ChannelHost(channels))
    monkeypatch.setattr(main, 'channel_adapters', {})
    monkeypatch.setattr(main, '_novelai', service.application, raising=False)
    service.application.channels = channels
    service.application.model_configuration = lambda identifier: main.model_configuration_store.get(identifier) if identifier else None
    service.application.model_adapter = lambda: main.model_adapter
    service.application.channel_media_supported = lambda account_id: bool(main.channel_adapters.get(account_id)
        and main.channel_adapters[account_id].capabilities.media)
    yield TestClient(main.app), service
