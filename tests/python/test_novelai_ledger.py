from concurrent.futures import ThreadPoolExecutor

import pytest

from novelai_fixtures import novelai_environment  # noqa: F401


def test_paid_intent_claim_survives_reopen_and_changed_call_cannot_charge_again(tmp_path):
    from backend.app.novelai_generation.ledger import GenerationLedger
    ledger = GenerationLedger(tmp_path / 'state.db')
    arguments = dict(role_id='role-171', source_scope='desktop', human_key='human-run-171',
        request_key='call-one', parameters={'seed': 171}, origin={}, service_revision=3,
        role_revision='role-grant', account_limit=20, role_limit=10)
    first = ledger.prepare(**arguments)
    assert ledger.prepare(**arguments)['attemptId'] == first['attemptId']
    with pytest.raises(ValueError, match='input turn'):
        ledger.prepare(**{**arguments, 'request_key': 'call-two'})
    ledger.mark_dispatching(first['attemptId'])
    reopened = GenerationLedger(tmp_path / 'state.db')
    history = reopened.list('role-171', 'desktop')['generations']
    assert len(history) == 1 and history[0]['status'] == 'unknown'
    assert reopened.prepare(**arguments)['attemptId'] == first['attemptId']
    assert reopened.usage('role-171')['accountAttempts'] == 1


def test_account_slot_and_daily_attempts_are_atomic(tmp_path):
    from backend.app.novelai_generation.ledger import GenerationLedger
    ledger = GenerationLedger(tmp_path / 'state.db')
    def submit(index):
        try:
            return ledger.prepare(role_id=f'role-{index}', source_scope='desktop', human_key=str(index),
                request_key=str(index), parameters={'seed': index}, origin={}, service_revision=1,
                role_revision='grant', account_limit=20, role_limit=10)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(submit, range(8)))
    assert len([item for item in attempts if item]) == 1
    winner = next(item for item in attempts if item)
    ledger.finish(winner['attemptId'], 'cancelled_before_dispatch')
    assert ledger.usage(winner['roleId'])['accountAttempts'] == 0
    assert submit(9) is not None
