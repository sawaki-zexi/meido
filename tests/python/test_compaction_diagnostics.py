import json
from datetime import datetime, timezone

from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_public_compaction_diagnostics_only_include_safe_fields(tmp_path):
    initialize_databases(tmp_path)
    store = SessionStore(tmp_path / 'sessions.db')
    store.create_run(AgentRun(runId='run-1', roleId='role-1', sessionKey='role:role-1',
        status='completed', startedAt=datetime.now(timezone.utc), modelSnapshot={'budgetDiagnostic': {
            'requests': 2, 'compaction': {'stage': 'failed', 'fallback': 'original',
                'rawPrompt': 'PRIVATE', 'summaryAttempts': [{'stopReason': 'length',
                'outcome': 'incomplete', 'maxTokens': 4000, 'elapsedMs': 20,
                'rawError': 'PRIVATE', 'usage': {'output_tokens': 4000, 'secret': 'PRIVATE'}},
                {'stopReason': 'PRIVATE', 'outcome': 'PRIVATE'}]}}}))
    result = store.budget_diagnostics()[0]
    assert result['compaction']['fallback'] == 'original'
    assert result['compaction']['summaryAttempts'][0]['stopReason'] == 'length'
    assert result['compaction']['summaryAttempts'][0]['usage'] == {'output_tokens': 4000}
    assert 'PRIVATE' not in json.dumps(result)
