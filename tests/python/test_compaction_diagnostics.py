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
                {'stopReason': 'PRIVATE', 'outcome': 'PRIVATE', 'fallbackReason': 'PRIVATE'},
                {'outcome': 'invalid_sources', 'source_message_ids': ['PRIVATE'], 'summary': 'PRIVATE',
                 'batch': 2, 'limit': 2000, 'shortening': True, 'countedTokens': 2100, 'localTokens': 2200,
                 'countSource': 'provider_output_upper_bound', 'fallbackReason': 'normalized_projection'},
                {'outcome': 'accepted', 'countedTokens': True, 'localTokens': -1, 'shortening': 'PRIVATE',
                 'countSource': 'PRIVATE', 'providerOutputTokens': 'PRIVATE'}]}}}))
    result = store.budget_diagnostics()[0]
    assert result['compaction']['fallback'] == 'original'
    assert result['compaction']['summaryAttempts'][0]['stopReason'] == 'length'
    assert result['compaction']['summaryAttempts'][0]['usage'] == {'output_tokens': 4000}
    assert 'PRIVATE' not in json.dumps(result)
    attempt = result['compaction']['summaryAttempts'][2]
    assert attempt['outcome'] == 'invalid_sources' and attempt['countedTokens'] == 2100
    assert attempt['countSource'] == 'provider_output_upper_bound'
    assert attempt['fallbackReason'] == 'normalized_projection'
    assert attempt['limit'] == 2000 and attempt['batch'] == 2 and attempt['shortening'] is True
    assert 'countedTokens' not in result['compaction']['summaryAttempts'][3]
    assert 'localTokens' not in result['compaction']['summaryAttempts'][3]
