import asyncio
import io
import json
import threading
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from novelai_fixtures import novelai_environment  # noqa: F401
from test_novelai_configuration import Credentials


def test_owner_generation_runs_in_real_runtime_and_reopens_owned_history(tmp_path):
    from backend.app.novelai_generation.application import NovelaiApplication
    from backend.app.novelai_generation.context import capture_tool_context
    from backend.app.novelai_generation.http import NovelaiHttp
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun, RoleInput
    from backend.app.role_store import RoleStore
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.task_runtime import TaskRuntime
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    roles = RoleStore(tmp_path / 'roles')
    tasks = TaskRuntime(tmp_path / 'tasks')
    picture = io.BytesIO()
    Image.new('RGB', (64, 64), '#171abc').save(picture, format='PNG')
    response = io.BytesIO()
    with zipfile.ZipFile(response, 'w') as archive:
        archive.writestr('image.png', picture.getvalue())
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(201)
            self.send_header('Content-Type', 'application/zip')
            self.end_headers()
            self.wfile.write(response.getvalue())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    app = NovelaiApplication(root=tmp_path / 'generation', tasks=tasks, sessions=sessions, roles=roles,
        channels=None, credentials=Credentials(), http=NovelaiHttp.for_test(f'http://127.0.0.1:{server.server_port}'))
    try:
        app.settings.set_token('fixture-secret-171')
        config = app.settings.save({'enabled': True, 'manualEnabled': True, 'confirmManualConsumption': True}, expected_revision=app.settings.public()['revision'])
        role = roles.create(RoleInput(name='生成角色', profile={'profile': 'fixture'},
            agentConfig={'novelai': {'manualEnabled': True, 'consentRevision': config['revision']}}))
        key = sessions.open_role_session(role.id).sessionKey
        sessions.create_run_with_messages(AgentRun(runId='owner-run', roleId=role.id, sessionKey=key,
            status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'}), 'blue sky')
        context = capture_tool_context(sessions, None, ToolContext(role.id, key, 'owner-run', CancellationToken(), 'generate-call'), role_id=role.id, is_owner=True)
        request = {'prompt': 'blue sky', 'mode': 'txt2img', 'seed': 171, 'size_preset': 'custom', 'custom_width': 64, 'custom_height': 64}
        result = asyncio.run(app.submit(context, request))
        assert result['status'] == 'succeeded' and len(result['attachments']) == 1
        assert len(requests) == 1 and requests[0]['parameters']['seed'] == 171
        assert asyncio.run(app.submit(context, request))['attemptId'] == result['attemptId']
        assert len(requests) == 1
        assert app.history(role.id)['generations'][0]['parameters']['seed'] == 171
        assert 'fixture-secret-171' not in str(result)
        assert not list((tmp_path / 'generation').rglob('*.response'))
    finally:
        asyncio.run(app.close())
        tasks.close()
        server.shutdown()
        server.server_close()
        server_thread.join()
