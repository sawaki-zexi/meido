"""New source permission cannot upgrade an original channel intent's grant."""
import json

import pytest

from backend.app.mcp_autonomy import declaration_hash
from test_proactive_channel_ack_boundaries import _case, _run
from test_proactive_targets_api import api, shell_api, platform, source  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_new_current_ack_permission_cannot_upgrade_the_original_unsupported_intent(
        api, monkeypatch, platform, source, tmp_path):
    declaration = {'name': 'ack', 'description': 'persistent fixed-expiry acknowledgement',
        'inputSchema': {'type': 'object', 'properties': {
            'event_ids': {'type': 'array', 'items': {'type': 'string'}},
            'ack_id': {'type': 'string'}, 'expires_at': {'type': ['number', 'null']},
            'ttl_hours': {'type': 'integer', 'minimum': 0}},
            'required': ['event_ids', 'ttl_hours'], 'additionalProperties': False}}
    with _case(api, monkeypatch, platform, source, tmp_path, original_ack=False) as case:
        # Before _run calls start(): the unmodified real fixture reads this
        # catalog in tools/list, but start saves the ordinary get-only grant.
        catalog_path = case.root / 'catalog.json'
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        assert all(tool['name'] != 'ack' for tool in catalog)
        catalog_path.write_text(json.dumps([*catalog, declaration]), encoding='utf-8')

        async def scenario():
            assert case.grant.ackTool is None and case.grant.ackDefinitionHash is None
            assert case.grant.ackContract == 'none' and case.grant.ackProofId is None
            original_inputs = json.loads(case.encoded)
            assert original_inputs['sourceGrants'] == [case.grant.model_dump(mode='json')]

            # Only the current HTTP configuration gets new permission. The
            # original frozen input and original source revision stay intact.
            requested = case.grant.model_copy(update={'ackTool': 'ack',
                'ackDefinitionHash': declaration_hash(declaration), 'ackContract': 'legacy-ttl'})
            current = case.replace_sources([requested.model_dump(mode='json')])['sourceGrants'][0]
            assert current['revision'] > case.grant.revision
            assert current['ackTool'] == 'ack' and current['ackContract'] == 'legacy-ttl'
            assert current['ackDefinitionHash'] == declaration_hash(declaration)
            assert current['ackProofId'] is None  # This valid first-attempt legacy grant needs no retry proof.
            assert {key: value for key, value in current.items() if key not in {
                'revision', 'ackTool', 'ackDefinitionHash', 'ackContract'}} == {
                key: value for key, value in case.grant.model_dump(mode='json').items() if key not in {
                    'revision', 'ackTool', 'ackDefinitionHash', 'ackContract'}}

            case.prune_original_snapshot()
            await case.rebuild()
            case.allow_original_projection()
            first_decision = case.now[0]
            for _ in range(3):
                await case.application.recover()
            original_ack = case.original_ack_row()
            assert original_ack['status'] == 'unsupported' and original_ack['attempts'] == 0
            assert original_ack['decision_at'] == first_decision
            assert original_ack['expires_at'] == first_decision + 604800
            assert json.loads(original_ack['source_grant'])['ackTool'] is None
            state = case.application.query(case.role)
            assert state['config']['sourceGrants'] == [current]
            assert state['deliveries'][0]['status'] == 'sent'
            assert state['deliveries'][0]['platformMessageId'] == '8301'
            case.assert_no_replay()  # Real business get_events once, ACK zero, model three, platform send once.
            case.assert_business_released()

            await case.rebuild()
            case.now[0] += 900
            for _ in range(3):
                await case.application.recover()
            assert case.original_ack_row() == original_ack
            assert case.application.query(case.role)['config']['sourceGrants'] == [current]
            case.assert_no_replay()
            case.assert_business_released()
            case.record('new-current-permission-does-not-upgrade-old-input',
                originalSourceRevision=case.grant.revision, currentSourceRevision=current['revision'],
                originalAckTool=case.grant.ackTool, currentAckTool=current['ackTool'])

        _run(case, scenario)
