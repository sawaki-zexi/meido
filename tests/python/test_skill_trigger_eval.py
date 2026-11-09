import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('skill_trigger_eval', Path(__file__).parents[2] / 'scripts/evals/skill_trigger_eval.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_fixed_semantic_matrix_has_required_coverage_and_scoring_needs_real_evidence():
    cases = evaluation.cases()
    assert len([case for case in cases if case['kind'] == 'positive']) == 12
    assert len([case for case in cases if case['kind'] == 'negative']) == 12
    assert len([case for case in cases if case['kind'] == 'adjacent']) == 6
    assert len(evaluation.template()) == 90
    with pytest.raises(ValueError):
        evaluation.score(evaluation.template())
    records = [{**item, 'model': 'deterministic-scoring-fixture', 'readSkillIds': item['expectedSkillIds'],
        'completed': True, 'evidence': 'fixture-only; not a real model measurement'} for item in evaluation.template()]
    report = evaluation.score(records)['models']['deterministic-scoring-fixture']
    assert report['initialSuggestedThresholdMet'] is True and report['completeMatrix'] is True
    partial = evaluation.score(records[:2])['models']['deterministic-scoring-fixture']
    assert partial['initialSuggestedThresholdMet'] is False
    with pytest.raises(ValueError):
        evaluation.score([records[0], records[0]])
