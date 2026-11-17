"""Recommendation orchestration with narrow collaborators and SQLite forbidden."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.graduation import Graduation
from yt_playlist.controllers.listening_layers import ListeningLayers
from yt_playlist.controllers.model_health import ModelHealth
from yt_playlist.controllers.model_preparation import ModelPreparation
from yt_playlist.controllers.taste import Taste
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import layers, rec_params

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('accepted', [True, False])
def test_model_publication_obeys_snapshot_and_acknowledges_only_captured_revision(accepted):
    model = Mock(spec_set=['publish_training_generation'])
    settings = Mock(spec_set=['publish_values'])
    modes = Mock(spec_set=['publish_generation'])
    refresh = Mock(spec_set=['revision', 'acknowledge'])
    refresh.revision.return_value = 'captured-revision'
    read_model = Mock(spec_set=['prepare_training_changes'])
    read_model.prepare_training_changes.return_value = 'files-prepared'
    reader = SimpleNamespace(rec_model=read_model)
    snapshot = SimpleNamespace(store=reader)
    snapshot.publish_ordered = Mock(side_effect=lambda publish: (publish() or True) if accepted else False)
    batch = {'vectors': {'tracks': [('key', b'vector')]}, 'settings': {'fit': 'new'}, 'modes': (['mode'], [])}
    prepare = Mock(return_value=batch)
    controller = ModelPreparation(snapshot=lambda: nullcontext(snapshot), refresh=refresh,
        model=model, settings=settings, modes=modes, prepare=prepare)
    order = Mock()
    for name, operation in [('vectors', model.publish_training_generation), ('settings', settings.publish_values),
                            ('modes', modes.publish_generation), ('ack', refresh.acknowledge)]:
        order.attach_mock(operation, name)
    result = controller.rebuild(100, keep_collaborative=True)
    prepare.assert_called_once_with(reader, 100, keep_collaborative=True)
    read_model.prepare_training_changes.assert_called_once_with(batch['vectors'], batch['settings'])
    if accepted:
        assert result is batch
        assert order.mock_calls == [call.vectors('files-prepared'), call.settings({'fit': 'new'}),
                                    call.modes(['mode'], []), call.ack('captured-revision')]
    else:
        assert result is None and not order.mock_calls


def test_cancelled_model_calculation_never_prepares_files_or_attempts_publication():
    read_model = Mock(spec_set=['prepare_training_changes'])
    snapshot = SimpleNamespace(store=SimpleNamespace(rec_model=read_model), publish_ordered=Mock())
    refresh = Mock(spec_set=['revision', 'acknowledge'])
    prepare = Mock(side_effect=WorkClosing('stopping'))
    controller = ModelPreparation(snapshot=lambda: nullcontext(snapshot), refresh=refresh,
        model=None, settings=None, modes=None, prepare=prepare)
    with pytest.raises(WorkClosing):
        controller.rebuild(100)
    read_model.prepare_training_changes.assert_not_called()
    snapshot.publish_ordered.assert_not_called()
    refresh.acknowledge.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'conflict', 'cancel'])
def test_health_report_publishes_only_completed_current_calculation(failure):
    reader = object()
    snapshot = SimpleNamespace(store=reader)
    snapshot.publish_ordered = Mock(side_effect=lambda publish: False if failure == 'conflict' else (publish() or True))
    reports = Mock(spec_set=['put_proposals'])
    calculate = Mock(return_value={'built_at': 100, 'inputs': 'stamp'})
    if failure == 'cancel':
        calculate.side_effect = WorkClosing('stopping')
    health = ModelHealth(snapshot=lambda: nullcontext(snapshot), input_key=lambda now: 'stamp',
        calculate=calculate, reports=reports, surface='model_health')
    if failure == 'cancel':
        with pytest.raises(WorkClosing):
            health.prepare(100)
        snapshot.publish_ordered.assert_not_called()
    elif failure == 'conflict':
        assert health.prepare(100) is None
    else:
        assert health.prepare(100) == {'built_at': 100, 'inputs': 'stamp'}
        reports.put_proposals.assert_called_once_with('model_health', calculate.return_value, 100)
    calculate.assert_called_once_with(reader, 100, 'stamp')
    if failure:
        reports.put_proposals.assert_not_called()


def test_taste_checkpoints_before_edits_and_preserves_genre_mute_bounds():
    settings = Mock(spec_set=['set_setting', 'delete_setting'])
    model = Mock(spec_set=['save_taste_settings', 'set_weight', 'reset_weights', 'restore_taste_settings'])
    taste = Taste(settings, model)
    order = Mock()
    order.attach_mock(model.save_taste_settings, 'checkpoint')
    order.attach_mock(model.set_weight, 'weight')
    taste.set_weight('genre:jazz', 0., 100)
    assert order.mock_calls == [call.checkpoint(rec_params.CHECKPOINT_SETTING, rec_params.checkpoint_keys(),
                                               100, replace=False),
                                call.weight('genre:jazz', 0., now=100, lo=0., hi=2.)]
    taste.set_param('now_min_events', -20, 101)
    settings.set_setting.assert_called_once_with('rec_param:now_min_events',
        str(rec_params.PARAMS_BY_NAME['now_min_events'].min))
    taste.reset_all(102)
    assert settings.delete_setting.call_args_list == [call(rec_params.SETTING_PREFIX + spec.name)
                                                      for spec in rec_params.PARAMS]
    model.reset_weights.assert_called_once_with()
    taste.restore_checkpoint()
    model.restore_taste_settings.assert_called_once_with(rec_params.CHECKPOINT_SETTING, rec_params.checkpoint_keys())


def graduation_inputs():
    totals, receipts = {}, {}
    signals = Mock(spec_set=['bump_theme', 'discount_theme', 'log_graduation',
                             'get_play_graduated_day', 'set_play_graduated_day'])
    def bump(axis, amount, now):
        totals[axis] = totals.get(axis, 0.) + amount
        return totals[axis]
    def discount(axis, amount):
        totals[axis] -= amount
    signals.bump_theme.side_effect = bump
    signals.discount_theme.side_effect = discount
    signals.get_play_graduated_day.side_effect = receipts.get
    signals.set_play_graduated_day.side_effect = receipts.__setitem__
    model = Mock(spec_set=['nudge_weight'])
    model.nudge_weight.return_value = 1.1
    params = {'theme_threshold': 1.2, 'graduate_up': 1.1, 'graduate_down': .9,
              'weight_revert_halflife_d': 30., 'source_w_play': 1.}
    return Graduation(signals, model, parameter=params.__getitem__), totals


def test_graduation_accumulates_crosses_and_discounts_only_one_threshold():
    controller, totals = graduation_inputs()
    controller.facet('genre:jazz', 1., 100)
    controller.model.nudge_weight.assert_not_called()
    controller.facet('genre:jazz', 1., 101)
    assert totals['genre:jazz'] == pytest.approx(.8)
    controller.model.nudge_weight.assert_called_once_with('genre:jazz', 1.1, lo=0., hi=2.,
                                                        now=101, revert_halflife_d=30.)
    controller.signals.log_graduation.assert_called_once_with('genre:jazz', 'event', 2., 1.1, 1.1, 101)
    controller.facet('era:1960', -2., 102)
    assert totals['era:1960'] == pytest.approx(-.8)
    assert controller.model.nudge_weight.call_args.args == ('era:1960', .9)


def test_daily_exposure_receipt_prevents_duplicate_learning_and_stamps_zero():
    controller, totals = graduation_inputs()
    controller.play_exposure({'genre:jazz': 2., 'genre:rock': 0.}, 100)
    controller.play_exposure({'genre:jazz': 2., 'genre:rock': 0.}, 101)
    assert controller.signals.bump_theme.call_count == controller.model.nudge_weight.call_count == 1
    assert controller.signals.set_play_graduated_day.call_count == 2
    assert totals['genre:jazz'] == pytest.approx(.8)
    controller.play_exposure({'genre:jazz': 2.}, 86400 + 100)
    assert controller.model.nudge_weight.call_count == 2


def test_slider_receipt_and_bounded_nudge_preserve_effective_preference():
    signals = Mock(spec_set=['bump_theme', 'discount_theme', 'log_graduation'])
    signals.bump_theme.return_value = 2.
    model = Mock(spec_set=['set_lean_graduated_day', 'get_weights', 'nudge_weight', 'set_lean'])
    model.get_weights.return_value = {'genre:jazz': 1.9}
    model.nudge_weight.return_value = 2.
    params = {'theme_threshold': 1.2, 'source_w_slider': 1., 'graduate_up': 1.1,
              'graduate_down': .9, 'weight_revert_halflife_d': 30.}
    controller = Graduation(signals, model, parameter=params.__getitem__)
    rows = [{'axis': 'genre:jazz', 'value': 1.5, 'last_graduated_day': None},
            {'axis': 'era:1990', 'value': 1., 'last_graduated_day': None},
            {'axis': 'genre:rock', 'value': 1.8, 'last_graduated_day': '1970-01-01'}]
    controller.slider_exposure(rows, 100)
    assert model.set_lean_graduated_day.call_count == 2
    signals.bump_theme.assert_called_once_with('genre:jazz', .5, 100)
    assert model.set_lean.call_args.args[1] * 2. == pytest.approx(1.5 * 1.9)
    signals.discount_theme.assert_called_once_with('genre:jazz', 1.2)


def test_listening_controller_reuses_supplied_evidence_and_deduplicates_tilt_inputs():
    captured = layers.RecentEvidence({'one': 100., 'two': 100.}, [], None, {})
    evidence = Mock(side_effect=AssertionError('supplied evidence must be reused'))
    plays = Mock(return_value=[{'identity_key': 'one', 'played_at': -1000.},
                              {'identity_key': 'one', 'played_at': 100.}])
    controller = ListeningLayers(parameter={'now_window_h': 6, 'now_min_events': 1,
                                            'session_halflife_h': 4}.__getitem__,
                                 evidence=evidence, plays=plays)
    assert controller.now_mode_mix(100, evidence=captured)[:2] == (None, 0)
    assert controller.session_mode_mix(100, evidence=captured)[:2] == (None, 0)
    assert controller.evidence_coverage(100, evidence=captured)['now']['total'] == 2
    evidence.assert_not_called()
    np.testing.assert_allclose(controller.session_tilt(100, np.array([[2., 0.]]), {'one': 0}), [1., 0.])
    plays.assert_called_once_with(100 - 24 * 3600)
