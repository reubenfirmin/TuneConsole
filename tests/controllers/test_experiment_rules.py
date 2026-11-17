"""Experiment decisions and prospective measurements over detached facts."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.model_followup import ModelFollowup
from yt_playlist.rec import experiments, model_selection, rec_params, scorecard
from yt_playlist.rec.scorecard_attribution import Attribution

pytestmark = pytest.mark.usefixtures('no_database')


def report():
    def arm(played, completion, skip):
        return dict(clusters=100, impressions=100,
                    assignment_provenance=dict(controlled=100, uncontrolled=0, legacy_cache=0),
                    cluster_ci95=dict(played=played, completion=completion, skip=skip))
    return dict(min_clusters_per_arm=100, live={
        'cohort:ppr': arm([.8, .9], [.8, .9], [.0, .1]),
        'cohort:cosine': arm([.1, .2], [.1, .2], [.8, .9])})


def test_detached_decision_preserves_supplied_evidence_and_bounds_history():
    evidence, history = report(), [dict(at=i) for i in range(205)]
    original = deepcopy((evidence, history))
    rows, record = experiments.decision_values(evidence, history, 'promote', 'cohort', '  Measured gain  ', 300)
    assert (evidence, history) == original
    assert record['powered'] and record['reason'] == 'Measured gain'
    assert record['live_evidence']['ppr'] == evidence['live']['cohort:ppr']
    persisted = dict(rows)
    assert persisted[rec_params.SETTING_PREFIX + 'ppr_ab_share'] == '1.0'
    saved = json.loads(persisted['ppr_experiment_decisions'])
    assert len(saved) == 200 and saved[0] == {'at': 6} and saved[-1] == record


@pytest.mark.parametrize('failure', ['power', 'missing-arm', 'uncontrolled', 'missing-control',
                                    'played', 'completion', 'skip', 'legacy'])
def test_detached_promotion_requires_each_existing_guardrail(failure):
    evidence = report()
    arm = evidence['live']['cohort:ppr']
    if failure == 'power':
        arm['clusters'] = 99
    elif failure == 'missing-arm':
        evidence['live'].pop('cohort:cosine')
    elif failure == 'uncontrolled':
        arm['assignment_provenance']['uncontrolled'] = 1
    elif failure == 'missing-control':
        arm['assignment_provenance']['controlled'] = 99
    elif failure in ('played', 'completion', 'skip'):
        arm['cluster_ci95'][failure] = [.0, 1.]
    else:
        arm['assignment_provenance']['legacy_cache'] = 1
    with pytest.raises(ValueError):
        experiments.decision_values(evidence, [], 'promote', 'cohort', 'Reason', 300)
    rows, record = experiments.decision_values(evidence, [], 'retire', 'cohort', 'Stop', 300)
    assert dict(rows)[rec_params.SETTING_PREFIX + 'ppr_ab_share'] == '0.0'
    assert record['decision'] == 'retire'


def experiment_inputs():
    rows = []
    for index, (controlled, probability) in enumerate(((True, .5), (True, True), (False, .5))):
        trace = dict(ranker='ppr', propensity=probability,
            experiment=dict(cohort='cohort', cluster='one', comparison_id=str(index),
                            ranker_controlled=controlled, ppr_policy={'algorithm': 'unknown-legacy-cache'}))
        rows.append(dict(id=index, served_at=100 + index, identity_key=f'key{index}',
                         surface='home', lane='wheelhouse', mode_id=1, provenance=json.dumps(trace)))
    return rows, Attribution(rows, [], [dict(identity_key='key0', played_at=110, is_generated=0)], [], [], 86400)


def test_detached_experiment_provenance_does_not_treat_repeat_views_as_clusters():
    inputs = experiment_inputs()
    shadows = [dict(ranker='cosine', rows=1000, opportunities=1000)]
    result = scorecard.experiment_facts(inputs, shadows, 90, 200, 100000)
    arm = result['live']['cohort:ppr']
    assert arm['clusters'] == 1 and arm['impressions'] == 3 and not arm['powered']
    assert arm['played'] == 1
    assert arm['assignment_provenance'] == dict(controlled=1, uncontrolled=2, legacy_cache=3)
    assert result['counterfactual'] == shadows and result['counterfactual_outcomes'] is None


def test_report_query_captures_only_the_mature_interval_without_a_transaction():
    ledger = Mock(spec_set=['experiment_inputs'])
    ledger.experiment_inputs.return_value = experiment_inputs(), []
    result = scorecard.experiment_report(SimpleNamespace(rec_ledger=ledger), 90, 200, 100000)
    ledger.experiment_inputs.assert_called_once_with(90, 200, 200)
    assert result['mature_before'] == 200


@pytest.mark.parametrize('current', [False, True])
def test_decision_command_guards_report_history_and_allocation_together(current):
    settings = Mock(spec_set=['get_setting', 'publish_values'])
    settings.get_setting.side_effect = lambda key: '[]' if key == 'ppr_experiment_decisions' else '.5'
    ledger = Mock(spec_set=['experiment_inputs'])
    ledger.experiment_inputs.return_value = experiment_inputs(), []
    snapshot = SimpleNamespace(store=SimpleNamespace(settings=settings, rec_ledger=ledger))
    snapshot.publish_ordered = Mock(side_effect=lambda publish: (publish() or True) if current else False)
    owner = SimpleNamespace(settings=settings, read_snapshot=lambda: nullcontext(snapshot))
    if current:
        result = experiments.decide(owner, 'retire', 'cohort', 'Stop', 100000)
        assert result['state'] == 'retired'
        assert settings.publish_values.call_args.args[0]['rec_param:ppr_ab_share'] == '0.0'
    else:
        with pytest.raises(ValueError, match='evidence or experiment settings changed'):
            experiments.decide(owner, 'retire', 'cohort', 'Stop', 100000)
        settings.publish_values.assert_not_called()
    assert call(rec_params.SETTING_PREFIX + 'ppr_ab_share') in settings.get_setting.call_args_list


def test_forecast_ranks_real_detached_vectors_on_one_unseen_intersection():
    keys = ['context', *(f'key{i:02}' for i in range(24))]
    previous = np.array([[1., 0.], *[(float(i), 1.) for i in range(24)]])
    selected = np.array([[1., 0.], *[(float(24 - i), 1.) for i in range(24)]])
    result = model_selection.forecast_vectors((keys, previous), (keys[:-1], selected[:-1]), {'context'})
    assert result['eligible'] == keys[1:-1]
    assert result['previous'] == keys[4:-1]
    assert result['selected'] == keys[1:21]
    assert model_selection.forecast_vectors((keys, previous), (keys, selected), {'unknown'}) is None
    assert model_selection.forecast_vectors((keys[:21], previous[:21]), (keys[:21], selected[:21]), {'context'}) is None


def test_followup_recomputes_late_imports_generated_exclusions_and_once_only_hits():
    forecasts = [dict(made_at=10, eligible=['a', 'b', 'repeat', 'generated'], previous=['a'], selected=['b']),
                 dict(made_at=20, eligible=['a', 'c', 'late'], previous=['a'], selected=['c']),
                 dict(made_at=30, eligible=['future'], previous=[], selected=['future'])]
    assert model_selection.followup_intervals(forecasts, 25) == [(0, 10, 20), (1, 20, 25)]
    intervals = [(0, 20, {'a', 'b', 'repeat', 'generated'}, {'repeat'}, {'generated'}),
                 (1, 25, {'a', 'c', 'late'}, {'late'}, set())]
    original = deepcopy((forecasts, intervals))
    result = model_selection.followup_facts(forecasts, intervals, 25)
    assert result['trials'] == 3 and result['previous_hits'] == 1 and result['selected_hits'] == 2
    assert result['generated_excluded'] == 1 and [row['trials'] for row in result['points']] == [2, 3]
    assert (forecasts, intervals) == original
    revised = model_selection.followup_facts(forecasts, [
        (0, 20, intervals[0][2], {'repeat', 'b'}, {'generated'}), intervals[1]], 25)
    assert revised['trials'] == 2 and revised['previous_hits'] == revised['selected_hits'] == 1


@pytest.mark.parametrize('blocked', [{'kind': 'daily'}, {'kind': 'product'}, {'followup': {'frozen_at': 100}}])
def test_followup_controller_does_not_restart_owned_or_frozen_runs(blocked):
    repository = Mock(spec_set=['selection_runs', 'model_forecasts', 'save_selection'])
    repository.selection_runs.return_value = [dict(id=1, status='retained', diagnostic_challenger={'dim': 2}, **blocked)]
    forecast = Mock()
    ModelFollowup(repository, Mock(), Mock(), Mock(), forecast=forecast).update(200)
    repository.model_forecasts.assert_not_called()
    repository.save_selection.assert_not_called()
    forecast.assert_not_called()


def test_followup_controller_records_forecast_after_calculation_with_its_actual_clock():
    repository = Mock(spec_set=['selection_runs', 'model_forecasts', 'save_selection', 'save_model_forecast'])
    run = dict(id=1, status='retained', previous={'dim': 2}, selected={'dim': 4}, diagnostic_challenger={'dim': 4})
    repository.selection_runs.return_value = [run]
    repository.model_forecasts.return_value = []
    settings = Mock(spec_set=['get_setting'])
    settings.get_setting.return_value = None
    forecast = Mock(return_value={'eligible': ['unseen'], 'previous': [], 'selected': ['unseen']})
    order = Mock()
    order.attach_mock(repository.save_selection, 'save')
    order.attach_mock(repository.save_model_forecast, 'forecast')
    command = ModelFollowup(repository, settings, Mock(), Mock(), forecast=forecast)
    command.update(200, now_fn=lambda: 250)
    forecast.assert_called_once_with({'dim': 2}, {'dim': 4}, 200)
    assert [event[0] for event in order.mock_calls] == ['save', 'forecast', 'save']
    repository.save_model_forecast.assert_called_once_with(1, 250, forecast.return_value)
    assert run['followup']['forecasts'] == 1
