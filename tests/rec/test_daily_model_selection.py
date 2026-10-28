"""Chronology, shared comparisons, daily admission and atomic serving changes."""
from contextlib import contextmanager
import sqlite3

import numpy as np
import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.store import Store
from yt_playlist.rec import daily_model_selection as daily, model_selection, model_setup
from yt_playlist.util.matching import identity_key


@pytest.fixture
def library(store, monkeypatch):
    now = 200 * daily.DAY
    account = store.identities.upsert_identity('main', 'cred', None, True)
    entries = [store.tracks.upsert_track(str(i), f'Song{i:03d}', 'Artist', None, None,
               created_at=-200 * daily.DAY) for i in range(160)]
    keys = [store.tracks.identity_key_for_video(str(i)) for i in range(160)]
    videos = {key: str(i) for i, key in enumerate(keys)}
    for offset in (0, 80):
        playlist = store.playlists.upsert_playlist(account, f'PL{offset}', 'Old curation', 80, 'old', -100 * daily.DAY)
        store.playlists.set_playlist_tracks(playlist, entries[offset:offset + 80])
    for key in keys[:20]:
        store.history.record_play_event(account, key, videos[key], -100 * daily.DAY)
    for index, key in enumerate(keys[20:80]):
        store.history.record_play_event(account, key, videos[key], now - 30 * daily.DAY + (index // 2) * daily.DAY + 100)
    assert set(map(tuple, store.conn.execute('SELECT identity_key,video_id FROM play_events'))) == {
        (key, videos[key]) for key in keys[:80]}
    original = [(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys]
    store.rec_model.replace_rec_vectors(original)
    store.settings.set_setting('rec_dim', '2')
    monkeypatch.setattr(model_setup, 'CONFIGS', [('svd', 2), ('svd', 3)])
    calls = []

    def fit(baskets, dim, method, weights):
        vocabulary = sorted({key for basket in baskets for key in basket})
        calls.append((dim, vocabulary, weights))
        good = set(keys[:20] + (keys[20:40] if dim == 3 else keys[120:140]))
        vectors = np.zeros((len(vocabulary), dim), dtype=np.float32)
        for index, key in enumerate(vocabulary):
            vectors[index, 0 if key in good else 1] = 1
        return vocabulary, vectors

    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', fit)
    return dict(store=store, now=now, account=account, keys=keys, videos=videos, original=original, calls=calls)


def complete(library, *, start=None):
    now = library['now'] if start is None else start
    result = None
    for step in range(12):
        result = daily.maintain(library['store'], now + step * 5)
        if result and result['status'] != 'running':
            return result
    pytest.fail(f'Comparison did not finish: {result}')


def test_all_prior_history_single_holdout_shared_inputs_and_full_refit(library):
    store, now = library['store'], library['now']
    with store.read_view() as reader:
        captured, _ = daily._capture(reader, now)
    # Training includes evidence older than any proposed 60/90-day limit.
    assert set(library['keys'][:20]) <= set(captured['context'])
    assert captured['cutoff'] == now - 30 * daily.DAY
    assert len(captured['held']) == 60
    assert len(set(captured['held'].values())) == 30
    result = complete(library)
    assert result['kind'] == 'daily' and result['status'] == 'applied'
    assert result['selected'] == {'method': 'svd', 'dim': 3}
    assert result['comparison']['windows'] == 1 and result['comparison']['trials'] == 60
    assert result['comparison']['selected_hits'] == 20
    assert [dim for dim, _, _ in library['calls']] == [2, 3, 3]
    assert library['calls'][0][1:] == library['calls'][1][1:]
    assert store.settings.get_setting('rec_dim') == '3'
    assert store.settings.get_setting('rec_fit_inputs')
    assert not daily.due(store, now + daily.DAY - 1)
    assert store.model_selection.restore_model_selection(result['id'], now + 100)
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])
    assert store.settings.get_setting('rec_dim') == '2'
    assert not daily.due(store, now + 2 * daily.DAY)


def test_training_excludes_holdout_sessions_weights_and_future_catalog(library):
    store, now, keys = library['store'], library['now'], library['keys']
    store.settings.set_setting('rec_cooc_weighting', '1')
    late = store.tracks.upsert_track('late', 'Late catalog', 'Artist', None, None, created_at=now - daily.DAY)
    late_key = identity_key('Late catalog', 'Artist')
    recent = store.playlists.upsert_playlist(library['account'], 'RECENT', 'New curation', 2, 'new', now - daily.DAY)
    store.playlists.set_playlist_tracks(recent, [late, store.tracks.upsert_track('extra', 'Extra', 'Artist', None, None)])
    store.history.record_play_event(library['account'], late_key, 'late', now - 100)
    with store.read_view() as reader:
        captured, _ = daily._capture(reader, now)
    vocabulary = {key for basket in captured['baskets'] for key in basket}
    assert late_key not in vocabulary and late_key not in captured['held']
    assert set(captured['weights']) == set(keys[:20])
    assert all(set(basket) <= set(keys) for basket in captured['baskets'])
    assert captured['coverage']['undated_relationship_baskets_excluded'] == ['album', 'artist']
    assert captured['unavailable_targets'] >= 1


def test_insufficient_activity_skips_all_fits(library):
    result = daily.maintain(library['store'], library['now'] + 90 * daily.DAY)
    assert result['status'] == 'insufficient'
    assert library['calls'] == []


@pytest.mark.parametrize(('initial_targets', 'new_targets', 'expected'), [
    (26, 24, 'ready'),
    (26, 30, 'ready'),
    (60, 24, 'waiting'),
])
def test_initial_insufficient_attempt_does_not_require_another_30_plays(
        store, monkeypatch, initial_targets, new_targets, expected):
    now = 200 * daily.DAY
    account = store.identities.upsert_identity('main', 'cred', None, True)
    entries = [store.tracks.upsert_track(str(i), f'Song{i:03d}', 'Artist', None, None,
               created_at=-200 * daily.DAY) for i in range(160)]
    keys = [store.tracks.identity_key_for_video(str(i)) for i in range(160)]
    videos = dict(zip(keys, map(str, range(160))))
    for offset in (0, 80):
        playlist = store.playlists.upsert_playlist(
            account, f'PL{offset}', 'Old curation', 80, 'old', -100 * daily.DAY)
        store.playlists.set_playlist_tracks(playlist, entries[offset:offset + 80])
    for key in keys[:20]:
        store.history.record_play_event(account, key, videos[key], -100 * daily.DAY)
    active_days = 6 if initial_targets == 26 else 8
    for index, key in enumerate(keys[20:20 + initial_targets]):
        store.history.record_play_event(account, key, videos[key],
            now - 15 * daily.DAY + (index % active_days) * daily.DAY + 100 + index)
    setup = model_setup.current_setup(store)
    vector = np.zeros(setup['dim'], dtype=np.float32)
    vector[0] = 1.
    original = {key: vector.tobytes() for key in keys}
    store.rec_model.replace_rec_vectors(list(original.items()))
    real_fit, fits = daily.training.build_vectors_from_baskets, []

    def counted_fit(*args, **kwargs):
        fits.append((kwargs['method'], kwargs['dim']))
        return real_fit(*args, **kwargs)

    # Keep the default grid, thresholds and numerical fitting. Only count fits;
    # synthetic incumbent vectors establish the modeled-track prerequisite.
    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', counted_fit)
    result = daily.maintain(store, now)
    if initial_targets == 26:
        assert result['status'] == 'insufficient'
        assert result['baseline'] == {'trials': 26, 'windows': 1, 'active_days': 6}
        assert fits == [] and dict(store.rec_model.get_rec_vectors()) == original
    else:
        for step in range(1, len(model_setup.CONFIGS) + 4):
            if result['status'] != 'running':
                break
            result = daily.maintain(store, now + step * 5)
        assert result['status'] in ('retained', 'applied') and result['published_vectors']
        assert len(fits) == len(model_setup.CONFIGS) + 1
    previous_id, fits_before = result['id'], len(fits)
    assert not daily.due(store, now + daily.DAY - 1)
    assert daily.readiness(store, now + daily.DAY - 1)['reason'] == 'The next daily comparison is scheduled.'
    next_at = now + daily.DAY + 3600
    added = keys[20 + initial_targets:20 + initial_targets + new_targets]
    for index, key in enumerate(added):
        store.history.record_play_event(account, key, videos[key], now + daily.DAY + 100 + index)
    with store.read_view() as reader:
        captured, _ = daily._capture(reader, next_at)
    assert len(captured['held']) == initial_targets + new_targets
    assert len(set(captured['held'].values())) >= daily.MIN_ACTIVE_DAYS
    assert daily.due(store, next_at)
    state = daily.readiness(store, next_at)
    assert state['state'] == expected
    result = daily.maintain(store, next_at)
    if expected == 'ready':
        assert result['status'] == 'running' and result['id'] != previous_id
        assert result['baseline']['trials'] == initial_targets + new_targets
        assert len(result['_evaluations']) == 1 and result['_evaluations'][0]['valid']
        assert len(fits) == fits_before + 1
    else:
        assert 'freshly played' in state['reason']
        assert result is None and len(fits) == fits_before
        assert store.model_selection.latest_daily_selection()['id'] == previous_id


def test_overlapping_holdout_does_not_satisfy_fresh_activity_gate(library):
    result = complete(library)
    store, now, keys = library['store'], library['now'], library['keys']
    count = len(library['calls'])
    assert daily.maintain(store, now + daily.DAY + 1) is None
    assert len(library['calls']) == count
    assert store.model_selection.latest_daily_selection()['id'] == result['id']
    # Fresh plays may be repeats of previously known songs; this gate measures
    # activity since the decision, separately from the held-out first-play score.
    for key in keys[:30]:
        store.history.record_play_event(library['account'], key, library['videos'][key], now + daily.DAY + 100)
    assert daily.readiness(store, now + daily.DAY + 101)['state'] == 'ready'


def test_candidates_and_full_refit_close_reader_before_fitting(library, monkeypatch):
    store = library['store']
    original_snapshot, original_fit = store.read_snapshot, daily.training.build_vectors_from_baskets
    readers, fits = [], []

    @contextmanager
    def snapshot(*args, **kwargs):
        with original_snapshot(*args, **kwargs) as value:
            readers.append(value.store.conn)
            yield value

    def fit(*args, **kwargs):
        fits.append(kwargs['dim'])
        with pytest.raises(sqlite3.ProgrammingError):
            readers[-1].execute('SELECT 1')
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(store, 'read_snapshot', snapshot)
    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', fit)
    assert complete(library)['status'] == 'applied'
    assert fits == [2, 3, 3]


def test_budget_retry_preserves_completed_candidate_predictions(library, monkeypatch):
    store, now = library['store'], library['now']
    assert daily.maintain(store, now)['status'] == 'running'
    original = daily._evaluate
    interrupted_fits = []

    def interrupt(setup, captured):
        interrupted_fits.append(setup['dim'])
        raise WorkBudgetExceeded()

    monkeypatch.setattr(daily, '_evaluate', interrupt)
    interrupted = daily.maintain(store, now + 5)
    assert interrupted_fits == [3]
    assert interrupted['status'] == 'running' and len(interrupted['_evaluations']) == 1
    model_selection.recover_interrupted(store)
    monkeypatch.setattr(daily, '_evaluate', original)
    assert complete(library, start=now + 65)['status'] == 'applied'
    assert [dim for dim, _, _ in library['calls']] == [2, 3, 3]


def test_changed_historical_inputs_discard_mixed_candidate_results(library):
    store, now = library['store'], library['now']
    first = daily.maintain(store, now)
    fingerprint = first['input_fingerprint']
    store.history.import_play_events(library['account'], [(library['keys'][20], '20', now - 80 * daily.DAY)])
    second = daily.maintain(store, now + 5)
    assert second['input_fingerprint'] != fingerprint
    assert len(second['_evaluations']) == 1
    assert [dim for dim, _, _ in library['calls']] == [2, 2]


def test_file_database_reopen_resumes_completed_candidate(library, tmp_path):
    path = tmp_path / 'library.db'
    with sqlite3.connect(path) as destination:
        library['store'].conn.backup(destination)
    first = Store(path)
    first.init_schema()
    try:
        run = daily.maintain(first, library['now'])
        assert len(run['_evaluations']) == 1
    finally:
        first.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    try:
        library['store'] = reopened
        model_selection.recover_interrupted(reopened)
        assert reopened.model_selection.latest_daily_selection()['id'] == run['id']
        assert complete(library, start=library['now'] + 5)['status'] == 'applied'
        assert [dim for dim, _, _ in library['calls']] == [2, 3, 3]
    finally:
        reopened.conn.close()


def test_full_refit_source_change_preserves_vectors_settings_and_restore_point(library, monkeypatch):
    store, now = library['store'], library['now']
    daily.maintain(store, now)
    daily.maintain(store, now + 5)
    original = daily.training.build_vectors_from_baskets
    changed = []

    def fit(*args, **kwargs):
        result = original(*args, **kwargs)
        store.settings.set_setting('rec_cooc_weighting', '1')
        changed.append(kwargs['dim'])
        return result

    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', fit)
    result = daily.maintain(store, now + 10)
    assert changed == [3]
    assert result['status'] == 'failed'
    assert store.settings.get_setting('rec_dim') == '2'
    assert not store.settings.get_setting('rec_selection_active_run')
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])


def test_kept_configuration_retrains_without_overwriting_active_restore(library, monkeypatch):
    store = library['store']
    setup = {'method': 'svd', 'dim': 2}
    prior = dict(status='applied', ran_at=library['now'] - daily.DAY, previous=setup, selected=setup, grid=[])
    restore_id = store.model_selection.start_selection(prior['ran_at'], prior)
    store.model_selection.save_selection(restore_id, prior, rows=library['original'])
    original = daily._evaluate

    def same_predictions(setup, captured):
        value = original(setup, captured)
        value['predictions'] = library['keys'][120:140]
        return value

    monkeypatch.setattr(daily, '_evaluate', same_predictions)
    result = complete(library)
    assert result['status'] == 'retained' and result['published_vectors']
    assert store.settings.get_setting('rec_selection_active_run') == str(restore_id)
    assert [dim for dim, _, _ in library['calls']] == [2, 3, 2]
    assert store.model_selection.restore_model_selection(restore_id, library['now'] + 100)
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])


def test_shutdown_does_not_publish_or_erase_saved_progress(library, monkeypatch):
    store, now = library['store'], library['now']
    daily.maintain(store, now)
    interrupted_fits = []

    def interrupt(setup, captured):
        interrupted_fits.append(setup['dim'])
        raise WorkClosing()

    monkeypatch.setattr(daily, '_evaluate', interrupt)
    with pytest.raises(WorkClosing):
        daily.maintain(store, now + 5)
    assert interrupted_fits == [3]
    assert len(store.model_selection.latest_daily_selection()['_evaluations']) == 1
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])


def test_day_votes_prevent_one_binge_from_creating_many_independent_wins():
    held = {f'key{i}': i // 10 for i in range(100)}
    captured = {'held': held}
    baseline = {'predictions': []}
    # Twenty correct guesses all from two days do not establish day-level support.
    candidate = {'predictions': list(held)[:20]}
    result = daily._compare(baseline, candidate, captured, .0125)
    assert result['eligible'] and result['trials'] == 100
    assert result['p'] == .25 and not result['promote']


@pytest.mark.parametrize('previous_run_id', [113, 10000])
def test_large_historical_run_counters_do_not_disable_future_model_switches(library, previous_run_id):
    store = library['store']
    old = {'kind': 'product', 'status': 'retained', 'previous': {'method': 'svd', 'dim': 2}}
    old_id = store.model_selection.start_selection(library['now'] - daily.DAY, old)
    # Model a retained high run ID after many legacy/failed runs, without
    # manufacturing thousands of unrelated historical records.
    with store.conn:
        store.conn.execute('UPDATE rec_model_selection_runs SET id=? WHERE id=?', (previous_run_id, old_id))
    result = complete(library)
    assert result['id'] > previous_run_id
    assert result['status'] == 'applied' and result['selected']['dim'] == 3
    assert result['comparison']['confidence_scope'] == 'comparison'
    assert result['comparison']['alpha'] == .05
    assert [dim for dim, _, _ in library['calls']] == [2, 3, 3]


def test_changed_decision_policy_restarts_saved_comparison(library, monkeypatch):
    current = daily.POLICY
    monkeypatch.setattr(daily, 'POLICY', 'all-prior-30-day-holdout-v1')
    first = daily.maintain(library['store'], library['now'])
    assert first['status'] == 'running' and len(first['_evaluations']) == 1
    monkeypatch.setattr(daily, 'POLICY', current)
    second = daily.maintain(library['store'], library['now'] + 5)
    assert second['policy'] == current and len(second['_evaluations']) == 1
    assert second['input_fingerprint'] != first['input_fingerprint']
    assert [dim for dim, _, _ in library['calls']] == [2, 2]
    assert complete(library, start=library['now'] + 10)['status'] == 'applied'


def test_generated_only_plays_are_excluded_and_disclosed(library):
    store, now, account = library['store'], library['now'], library['account']
    store.playlists.upsert_playlist(account, 'GENERATED', 'Generated', 0, 'generated', now)
    store.playlists.mark_generated_playlist('GENERATED')
    key = library['keys'][90]
    store.history.record_play_event(account, key, library['videos'][key], now - 100, playlist_ytm_id='GENERATED')
    with store.read_view() as reader:
        captured, _ = daily._capture(reader, now)
    assert key not in captured['held']
    assert captured['generated_excluded'] == 1


def test_pausing_during_full_refit_prevents_publication(library, monkeypatch):
    store, now = library['store'], library['now']
    daily.maintain(store, now)
    daily.maintain(store, now + 5)
    original = daily.training.build_vectors_from_baskets
    paused = []

    def fit(*args, **kwargs):
        value = original(*args, **kwargs)
        store.settings.set_setting('rec_selection_paused', '1')
        paused.append(kwargs['dim'])
        return value

    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', fit)
    assert daily.maintain(store, now + 10)['status'] == 'failed'
    assert paused == [3]
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])
    assert not daily.due(store, now + 2 * daily.DAY)


def test_failure_after_vector_writes_rolls_back_the_whole_publication(library, monkeypatch):
    store, now = library['store'], library['now']
    daily.maintain(store, now)
    daily.maintain(store, now + 5)
    original = store.model_selection.publish_selection
    published_rows = []

    def fail_after_writes(run_id, encoded, rows, selected):
        original(run_id, encoded, rows, selected)
        if rows is not None:
            published_rows.append(len(rows))
            raise ValueError('controlled publication failure')

    monkeypatch.setattr(store.model_selection, 'publish_selection', fail_after_writes)
    assert daily.maintain(store, now + 10)['status'] == 'failed'
    assert published_rows == [len(library['keys'])]
    assert dict(store.rec_model.get_rec_vectors()) == dict(library['original'])
    assert store.settings.get_setting('rec_dim') == '2'
    assert not store.settings.get_setting('rec_selection_active_run')


def test_tied_best_alternatives_keep_the_incumbent(library, monkeypatch):
    monkeypatch.setattr(model_setup, 'CONFIGS', [('svd', 2), ('svd', 3), ('svd', 4)])
    original = daily._evaluate
    compared = []

    def tie(setup, captured):
        compared.append(setup['dim'])
        measured = original(setup, captured)
        if setup['dim'] in (3, 4):
            measured['predictions'] = library['keys'][20:40]
        return measured

    monkeypatch.setattr(daily, '_evaluate', tie)
    result = complete(library)
    assert compared == [2, 3, 4]
    assert result['status'] == 'retained' and result['selected']['dim'] == 2


def test_legacy_gate_ends_but_held_projections_survive(library):
    from yt_playlist.rec import product_comparison
    store, now = library['store'], library['now']
    setup = {'method': 'svd', 'dim': 2}
    old = dict(kind='product', status='running', phase='observing', ran_at=now - daily.DAY,
               previous=setup, selected=setup, grid=[])
    run_id = store.model_selection.start_selection(old['ran_at'], old)
    current = dict(run_id=run_id, phase='observing')
    store.rec_proposals.put_proposals(product_comparison.SURFACE, current, now)
    surface = product_comparison.pair_surface(run_id, 'incumbent', 'mixes')
    held = {'retained': 'frozen held projection'}
    store.rec_proposals.put_proposals(surface, held, now)
    store.settings.set_setting('rec_selection_live_run', str(run_id))
    daily.maintain(store, now)
    assert store.model_selection.selection_run(run_id)['status'] == 'retained'
    assert product_comparison.state(store)['phase'] == 'retained'
    assert product_comparison.assignment(store, 'mixes', 'new', now) is None
    assert store.rec_proposals.get_proposals(surface) == held
    assert not store.settings.get_setting('rec_selection_live_run')
