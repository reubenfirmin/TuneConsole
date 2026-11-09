"""Typed fallback ranks and trial controls retain their separate lifecycles."""
import sqlite3

import pytest

from tests.core.test_tv_state_migration import store, cutover
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import _numbered_input, native_sql_calls, without_query_timing
from yt_playlist.core import model_comparison_state
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.rec import library_pool, product_comparison


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Typed recommendation state has no HTTP lifecycle dependency."""


@pytest.fixture
def publication_predecessor(tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # The unchanged v94 input is below publication. Keep the actual v95
    # cutover and its subsequent startup admission in the consuming test.
    with _numbered_input(94, tmp_path, monkeypatch) as (database, _version):
        yield database


def test_fresh_clicked_build_uses_staged_rank_publication_and_current_filters(publication_predecessor, monkeypatch):
    from tests.rec.test_choice_bandit import modes
    from yt_playlist.rec.surfaces import ForYouItem
    store = publication_predecessor
    cutover(store)
    modes(store)
    store._snapshot_revisions = Revisions(store.conn)
    seed_publication_subjects(store, [dict(key=f'song {index}|artist', title=f'Song {index}',
        artist='Artist', video_id=f'video{index}') for index in range(8)])
    ranked = []
    for index in range(8):
        ranked.append(ForYouItem(f'Song {index}', 'Artist', '', f'video{index}', None, 0,
                                'Controlled scorer result', key=f'song {index}|artist'))
    def score(reader, *args, **kwargs):
        reader.recommendations.head('home')
        store.modes.log_impressions(1, [('wheelhouse', 1)], 100)
        return ranked
    monkeypatch.setattr(library_pool.surfaces, 'for_you', score)
    parameters = {'lane': 'wheelhouse', 'epoch': 1, 'include_genres': [], 'avoid_genres': []}
    library_pool.prepare(store, 100, parameters)
    result = store.rec_proposals.get_proposals('home_pool:wheelhouse')
    assert result and result['parameters'] == parameters and len(result['tracks']) == 8
    assert store.recommendations.pool('home_pool:wheelhouse', 'all', 'wheelhouse')['eligible_count'] == 8
    selected = library_pool.pick(store, 100, parameters, exclude_keys=['song 0|artist'])
    assert selected and all(track['key'] != 'song 0|artist' for track in selected)
    # Feedback arriving after publication must change eligibility immediately;
    # the retained, complete ranking is still the original eight candidates.
    for index in range(7):
        store.rec_model.record_feedback('for_you', f'song {index}|artist', 'dismiss', now=101)
    selected = library_pool.pick(store, 102, parameters)
    assert [track['key'] for track in selected] == ['song 7|artist']
    assert store.rec_proposals.get_proposals('home_pool:wheelhouse') == result


def test_trial_monitor_and_held_assignment_are_exact_typed_state_with_explicit_run_custody(store):
    run = store.model_selection.start_selection(1, {'kind': 'product', 'previous': {'method': 'svd', 'dim': 2}})
    plan = {'phase': 'observing', 'run_id': run, 'attempt': 1, 'policy': product_comparison.POLICY,
            'previous': {'method': 'svd', 'dim': 2}, 'challenger': {'method': 'svd', 'dim': 3},
            'generation': 'frozen', 'vectors': 'captured-artifact', 'alpha': .025,
            'started_at': 1, 'exposure_until': 100, 'decision_at': 101}
    monitor = {'run_id': run, 'policy': product_comparison.POLICY, 'setup': {'method': 'svd', 'dim': 3},
               'baseline': [.1, .5, 1.], 'complete': False, 'next_at': 200}
    assignment = {'epoch': 10, 'comparison': {'run_id': run, 'arm': 'challenger', 'format': 'albums',
                  'probability': .5, 'window': '10', 'unit': '0', 'controlled': True}}
    sources = {'model_live_comparison': plan, 'model_listening_monitor': monitor, 'model_album_assignment': assignment}
    for surface, value in sources.items():
        store.rec_proposals.put_proposals(surface, value, None)
    cutover(store)
    for surface, value in sources.items():
        assert store.rec_proposals.get_proposals(surface) == value
        assert not store.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
    assert [entry[0] for entry in store.conn.execute('SELECT value FROM model_monitor_baseline ORDER BY position')] == [.1, .5, 1.]
    with pytest.raises(sqlite3.IntegrityError), store.conn.atomic():
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (run,))
    with pytest.raises(sqlite3.IntegrityError), store.conn.atomic():
        store.conn.execute('UPDATE model_comparison_plan SET run_ref=NULL')
    with pytest.raises(InterruptedError), store.conn.atomic():
        model_comparison_state.write(store.conn, 'model_live_comparison', {**plan, 'phase': 'retained'}, 2)
        raise InterruptedError('Before control publication commit')
    assert store.rec_proposals.get_proposals('model_live_comparison') == plan
    with store.conn.atomic():
        model_comparison_state.clear(store.conn)
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (run,))
    assert store.rec_proposals.get_proposals('model_live_comparison') is None
