"""Complete Taste queries and changes over narrow ports, without SQLite or a web app."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.taste import COMPARISON_SETTING, Taste, comparison_rows
from yt_playlist.controllers.taste_queries import TasteQueries

pytestmark = pytest.mark.usefixtures('no_database')


def query():
    ports = SimpleNamespace(
        model=Mock(spec_set=['get_weights', 'weight_records', 'rec_vectors_count', 'list_dislikes', 'feedback_summary']),
        tracks=Mock(spec_set=['tracks_total', 'genre_distribution', 'tracks_by_keys']),
        discovery=Mock(spec_set=['discovered_tracks_by_keys']), proposals=Mock(spec_set=['get_proposals']),
        modes=Mock(spec_set=['list_modes']), evidence=Mock(spec_set=['rec_baskets']),
        history=Mock(spec_set=['spotify_history_stats']), settings=Mock(spec_set=['get_setting']),
        readings=Mock(spec_set=['mix_choices', 'overview', 'group_view', 'group_checkpoint', 'group_progress',
            'group_policy', 'engine', 'maintenance', 'selection', 'autotune', 'album_feedback', 'transparency']),
        breadth=Mock(), ranked=Mock(), tuning=Mock())
    controller = TasteQueries(ports.model, ports.tracks, ports.discovery, ports.proposals, ports.modes,
        ports.evidence, ports.history, ports.settings, readings=ports.readings, parameter=lambda name: 30.,
        breadth=ports.breadth, ranked=ports.ranked, tuning=ports.tuning)
    return controller, ports


def test_overview_only_acquires_visible_facts_and_reuses_choice_profiles():
    controller, ports = query()
    ports.tracks.tracks_total.return_value = 10
    ports.tracks.genre_distribution.return_value = {'jazz': 4}
    ports.model.rec_vectors_count.return_value = 8
    ports.readings.mix_choices.return_value = {'rows': [{'mode_id': 7, 'profile': 'captured'}]}
    result = controller.panel('overview', 100)
    assert result['coverage'] == .4 and result['vectors'] == 8
    ports.readings.overview.assert_called_once_with(100, profiles={7: 'captured'})
    assert [entry[0] for entry in ports.readings.mock_calls] == ['mix_choices', 'overview', 'group_view']
    assert not ports.modes.mock_calls and not ports.evidence.mock_calls and not ports.ranked.mock_calls


def test_health_joins_kpis_and_selection_without_loading_listening_models():
    controller, ports = query()
    ports.tracks.tracks_total.return_value = 0
    ports.tracks.genre_distribution.return_value = {}
    ports.breadth.return_value = {'breadth': .5, 'n_tagged': 2, 'n_families': 3}
    ports.evidence.rec_baskets.return_value = ['basket']
    ports.readings.engine.return_value = {'dimensions': 64}
    ports.readings.maintenance.return_value = {'state': 'ready'}
    result = controller.panel('health', 100)
    assert result['coverage'] == 0 and result['dimensions'] == 64 and result['baskets'] == 1
    assert result['breadth_tagged'] == 2 and result['breadth_families'] == 3
    ports.readings.selection.assert_called_once_with(100, limit=6)
    assert not ports.model.mock_calls and not ports.modes.mock_calls
    ports.readings.transparency.assert_not_called()
    ports.readings.mix_choices.assert_not_called()


@pytest.mark.parametrize('adaptive', [False, True])
def test_profile_bounds_metadata_to_representatives_and_skips_legacy_modes_when_adaptive(adaptive):
    controller, ports = query()
    ports.readings.group_checkpoint.return_value = adaptive
    ports.modes.list_modes.return_value = [{'label': 'Jazz', 'size': 20, 'first_seen': 90,
                                           'rep_keys': ['known', 'unknown']}]
    ports.tracks.tracks_by_keys.return_value = {'known': {'title': 'Known'}}
    result = controller.panel('profile', 100)
    if adaptive:
        assert result['modes'] == [] and not ports.modes.mock_calls and not ports.tracks.mock_calls
    else:
        assert result['modes'] == [{'label': 'Jazz', 'size': 20, 'fresh': True, 'tracks': [{'title': 'Known'}]}]
        ports.tracks.tracks_by_keys.assert_called_once_with(['known', 'unknown'])


def test_feedback_hydrates_only_missing_library_rows_and_keeps_unknown_and_expired_bans():
    controller, ports = query()
    ports.model.list_dislikes.return_value = [{'item_key': 'known|Artist', 'until': 99},
                                              {'item_key': 'missing|', 'until': None}]
    ports.model.feedback_summary.return_value = {'like': 3, 'play_grad': 7}
    ports.tracks.tracks_by_keys.return_value = {'known|Artist': {'title': 'Library title'}}
    ports.discovery.discovered_tracks_by_keys.return_value = {}
    result = controller.panel('feedback', 100)
    assert result['feedback'] == {'like': 3}
    assert result['bans'][0]['title'] == 'Library title' and result['bans'][0]['until'] == 99
    assert result['bans'][1]['artist'] == 'Unknown artist' and result['bans'][1]['until'] is None
    ports.discovery.discovered_tracks_by_keys.assert_called_once_with(['missing|'])
    ports.readings.album_feedback.assert_called_once_with()


def test_recommendations_expose_saved_and_effective_weights_without_mutating_records():
    controller, ports = query()
    ports.breadth.return_value = {'families': {'jazz': .7, 'rock': .3}}
    ports.model.get_weights.return_value = {'genre:jazz': 1.1}
    records = {'genre:jazz': {'weight': 1.5, 'updated_at': 86400}}
    ports.model.weight_records.return_value = records
    ports.readings.group_checkpoint.return_value = True
    result = controller.panel('recommendations', 2 * 86400)
    assert result['genres'][0] == {'family': 'jazz', 'share': .7, 'weight': 1.1, 'saved_weight': 1.5,
                                    'age_days': 1., 'has_record': True}
    assert result['genres'][1]['age_days'] is None and not result['genres'][1]['has_record']
    assert result['controls']['adaptive'] is True
    ports.tuning.assert_called_once_with(2 * 86400)
    assert records['genre:jazz']['weight'] == 1.5


@pytest.mark.parametrize('raw', [None, 'invalid', '{}', '[{}]'])
def test_absent_or_invalid_baseline_does_not_acquire_current_ranking(raw):
    controller, ports = query()
    ports.settings.get_setting.return_value = raw
    assert controller.comparison(100) == {'comparison': None}
    assert not ports.ranked.mock_calls and not ports.model.mock_calls


def ranked_row(key, *, rank=1, lane='explore', weight=1., cohort=True):
    return {'key': key, 'rank': rank, 'score': .5, 'trace': {'durable': .2}, 'lane': lane,
            'lane_weight': weight, 'in_cohort': cohort, 'title': key}


def test_baseline_command_captures_compact_identity_then_query_includes_exact_saved_cohort():
    settings = Mock(spec_set=['set_setting', 'get_setting'])
    ranked = Mock(return_value=[ranked_row('saved|Artist')])
    command = Taste(settings, None, ranked=ranked)
    command.capture_baseline(100)
    name, encoded = settings.set_setting.call_args.args
    assert name == COMPARISON_SETTING
    baseline = json.loads(encoded)
    assert set(baseline[0]) == {'key', 'rank', 'score', 'trace', 'lane', 'lane_weight'}
    assert baseline[0]['key'] == 'saved|Artist'
    controller, ports = query()
    ports.settings.get_setting.return_value = encoded
    ports.ranked.return_value = [ranked_row('saved|Artist')]
    ports.model.get_weights.return_value = {}
    result = controller.comparison(101)
    ports.ranked.assert_called_once_with(101, ['saved|Artist'])
    assert result['comparison'][0]['status'] == 'stayed'
    assert result['comparison'][0]['rank_delta'] == 0


def test_detached_comparison_keeps_entries_exits_lane_fallback_and_trace_deltas():
    baseline = [ranked_row('old', rank=2, weight=1.), ranked_row('stays', rank=3)]
    current = [ranked_row('new'), ranked_row('old', rank=None, lane='', weight=None, cohort=False),
               {**ranked_row('stays', rank=2), 'score': .8, 'trace': {'durable': .6, 'axes': {'genre': 1.2}}}]
    captured = copy.deepcopy(current)
    result = comparison_rows(baseline, current, {'lane:explore': 1.4})
    assert [row['status'] for row in result] == ['entered', 'exited', 'stayed']
    assert result[1]['lane_weight'] == 1.4 and result[1]['rank_delta'] is None
    assert result[2]['rank_delta'] == 1 and result[2]['score_delta'] == pytest.approx(.3)
    assert result[2]['changes'][0]['label'] == 'Saved taste match'
    assert current == captured


def test_feedback_and_selection_commands_preserve_effects_and_reject_unsafe_transitions():
    model = Mock(spec_set=['clear_feedback', 'unban'])
    selection = Mock(spec_set=['active_run_id', 'request_restore', 'request_pause', 'restore_pending', 'request_resume'])
    command = Taste(None, model, selection=selection)
    command.clear_feedback()
    command.unban('key', 100)
    assert model.mock_calls == [call.clear_feedback(), call.unban('key', 100)]
    selection.active_run_id.return_value = None
    with pytest.raises(ValueError, match='no model selection'):
        command.restore_selection()
    selection.request_restore.assert_not_called()
    selection.active_run_id.return_value = '7'
    command.restore_selection()
    selection.request_restore.assert_called_once_with('7')
    command.pause_selection()
    selection.request_pause.assert_called_once_with()
    selection.restore_pending.return_value = True
    with pytest.raises(ValueError, match='finish restoring'):
        command.resume_selection()
    selection.request_resume.assert_not_called()
    selection.restore_pending.return_value = False
    command.resume_selection()
    selection.request_resume.assert_called_once_with()
