"""Chronology, shared comparisons, daily admission and atomic serving changes."""
from contextlib import closing, contextmanager, nullcontext

import sqlite3

import numpy as np
import pytest

from tests.prepared_fixture_support import prepared_template, roots
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema,
    use_native_sql_calls, without_query_timing,
)
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core import embedding_files, schema_inspection
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import daily_model_selection as daily, model_selection, model_setup
from yt_playlist.util.matching import identity_key


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # Model-selection cases do not create an app or a sync worker.
    # Avoid importing the entire web application for an unused global fixture.
    pass


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def library(monkeypatch, request, native_sql_calls):
    if 'daily_library_template' in request.fixturenames:
        # Restore the populated input once, without first cloning empty SQL.
        store = Store(':memory:')
        with closing(store.conn), schema_inspection.reuse_tables(store.conn):
            value = _library(store, monkeypatch, request.getfixturevalue('daily_library_template'))
            # The model workflow owns real guarded snapshots. Its immutable
            # seed and the reporting-only sibling do not open snapshots.
            schema = request.getfixturevalue('_pristine_current_schema')
            store._snapshot_revisions = schema.bind_revisions(store.conn)
            yield value
    else:
        store = request.getfixturevalue('store')
        yield configured_library(store, monkeypatch, _seed_library(store, _seed_catalog(store)))


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema):
    with _file_consumer_store(tmp_path, _pristine_current_schema) as database:
        yield database


@pytest.fixture(scope='module')
def daily_library_template(_pristine_current_schema):
    def seed(source):
        # This completed lower input owns real ingestion SQL and vector files.
        # SQL diagnostics have independent owners.
        # The cold input builder has one writer. Owned repository reads reuse
        # that connection; actual ingestion/vector writes still commit normally.
        with pytest.MonkeyPatch.context() as patch, source._lock, schema_inspection.reuse_tables(source.conn):
            use_native_sql_calls(patch)
            patch.setattr(source, 'read_view', lambda **kwargs: nullcontext(source))
            return _seed_library(source, _seed_catalog(source))

    with prepared_template(_pristine_current_schema, seed, snapshot_guards=False) as template:
        yield template


@pytest.fixture
def daily_file_library(tmp_path, daily_library_template, native_sql_calls):
    # A populated clone supplies its own SQL, files and genuine TEMP guards.
    store = Store(tmp_path / 'daily-library.db')
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        value = daily_library_template.restore(store)
        store._bind_repositories()
        yield store, value


@pytest.fixture(scope='module')
def daily_catalog_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, _seed_catalog) as template:
        yield template


def _seed_catalog(store):
    now = 200 * daily.DAY
    with atomic_write(store):
        account = store.accounts.upsert_account('main', 'cred', None, True)
        keys = [identity_key(f'Song{i:03d}', 'Artist') for i in range(160)]
        seed_publication_subjects(store, [dict(key=key, video_id=str(i),
            title=f'Song{i:03d}', artist='Artist', created_at=-200 * daily.DAY)
            for i, key in enumerate(keys)])
        entries = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
        videos = {key: str(i) for i, key in enumerate(keys)}
        for offset in (0, 80):
            playlist = store.playlists.upsert_playlist(account, f'PL{offset}', 'Old curation', 80, 'old', -100 * daily.DAY)
            store.playlists.set_playlist_tracks(playlist, entries[offset:offset + 80])
    store.history.import_listening_events(account,
        [(key, videos[key], -100 * daily.DAY) for key in keys[:20]], source='live')
    return dict(now=now, account=account, keys=keys, videos=videos)


def _seed_library(store, value):
    now, account, keys, videos = (value[name] for name in ('now', 'account', 'keys', 'videos'))
    store.history.import_listening_events(account, [
        (key, videos[key], now - 30 * daily.DAY + (index // 2) * daily.DAY + 100)
        for index, key in enumerate(keys[20:80])], source='live')
    assert set(map(tuple, store.conn.execute('SELECT identity_key,video_id FROM play_event_subjects'))) == {
        (key, videos[key]) for key in keys[:80]}
    original = [(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys]
    store.rec_model.replace_rec_vectors(original)
    store.settings.set_setting('rec_dim', '2')
    return dict(now=now, account=account, keys=keys, videos=videos, original=original)


def _library(store, monkeypatch, template):
    value = template.restore(store)
    store._bind_repositories()
    return configured_library(store, monkeypatch, value)


def configured_library(store, monkeypatch, value):
    keys = value['keys']
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
    return dict(value, store=store, calls=calls)


def complete(library, *, start=None):
    now = library['now'] if start is None else start
    result = None
    for step in range(12):
        result = daily.maintain(library['store'], now + step * 5)
        if result and result['status'] != 'running':
            return result
    pytest.fail(f'Comparison did not finish: {result}')


@pytest.fixture
def comparison_reporting_reads(library, daily_library_template, daily_file_library, monkeypatch):
    # Copy assertions have one synchronous owner and no concurrent writes.
    # Supply their existing SQL connections below reporting reads; daily
    # comparison still opens its actual guarded snapshots independently.
    @contextmanager
    def owned_reader(target):
        with target._lock:
            yield target

    for target in (library['store'], daily_library_template.source, daily_file_library[0]):
        monkeypatch.setattr(target, 'read_view', lambda target=target, **kwargs: owned_reader(target))


@pytest.mark.usefixtures('native_sql_calls', 'comparison_reporting_reads')
def test_all_prior_history_single_holdout_shared_inputs_and_full_refit(
        library, daily_library_template, daily_file_library, monkeypatch):
    store, now = library['store'], library['now']
    file_backed_store, copied = daily_file_library
    # Compare the real seed and both restore modes before any selection work.
    with monkeypatch.context() as patch:
        sibling = configured_library(file_backed_store, patch, copied)
        for current in (daily_library_template.source, store, file_backed_store):
            # Compare all the same SQL/vector inputs through one real reader
            # per copy. No writes occur between these complete-copy assertions.
            with current.read_view() as reader:
                assert reader.conn.execute('SELECT count(*) FROM tracks').fetchone()[0] == 160
                for offset in (0, 80):
                    playlist = reader.playlists.get_playlist_for_account(library['account'], f'PL{offset}')
                    assert len(reader.playlists.get_playlist_track_ids(playlist.id)) == 80
                    assert reader.playlists.get_playlist_track_keys(playlist.id) == set(library['keys'][offset:offset + 80])
                assert list(map(tuple, reader.conn.execute('SELECT account_id,video_id,played_at '
                    'FROM play_event_subjects ORDER BY id'))) == [
                        (library['account'], str(i), -100 * daily.DAY) for i in range(20)] + [
                        (library['account'], str(i), now - 30 * daily.DAY + ((i - 20) // 2) * daily.DAY + 100)
                        for i in range(20, 80)]
                assert reader.rec_model.get_rec_vectors() == library['original']
                assert reader.settings.get_setting('rec_dim') == '2'
                assert not reader.settings.get_setting('rec_fit_inputs')
                assert reader.model_selection.latest_daily_selection() is None
        copied_roots = [roots(target.conn) for target in (store, file_backed_store)]
        for name, source in roots(daily_library_template.source.conn).items():
            destinations = [directories[name] for directories in copied_roots]
            assert len(set([source, *destinations])) == 3
            for path in source.rglob('*'):
                if path.is_file():
                    for destination in destinations:
                        copied = destination / path.relative_to(source)
                        assert copied.read_bytes() == path.read_bytes()
                        assert not copied.samefile(path)
        assert len(list(embedding_files.references(file_backed_store.conn))) == 2
        weights = {library['keys'][0]: 1.4}
        vocabulary, first = daily.training.build_vectors_from_baskets([sibling['keys']], 2, 'svd', weights)
        _, second = daily.training.build_vectors_from_baskets([sibling['keys']], 2, 'svd', {})
        first[:] = 0
        assert second.sum() == len(vocabulary) == 160
        assert len(sibling['calls']) == 2 and sibling['calls'][0][2] is weights
        sibling['keys'].clear()
        sibling['videos'].clear()
        sibling['original'].clear()
        sibling['calls'].clear()
        file_backed_store.settings.set_setting('private', 'sibling')
    assert library['calls'] == []
    assert len(library['keys']) == len(library['videos']) == len(library['original']) == 160
    assert len(daily_library_template.result['keys']) == len(daily_library_template.result['videos']) == 160
    assert len(daily_library_template.result['original']) == 160
    assert store.settings.get_setting('private') is None
    assert daily_library_template.source.settings.get_setting('private') is None
    store.rec_model.set_weight('genre:jazz', 1.4, now=now - 1)
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
    assert store.rec_model.weight_records()['genre:jazz']['weight'] == 1.4
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


@pytest.mark.usefixtures('native_sql_calls')
@pytest.mark.parametrize(('initial_targets', 'new_targets', 'expected'), [
    (26, 24, 'ready'),
    (26, 30, 'ready'),
    (60, 24, 'waiting'),
])
def test_initial_insufficient_attempt_does_not_require_another_30_plays(
        store, monkeypatch, initial_targets, new_targets, expected,
        request, _pristine_current_schema):
    # Scheduling consumes synchronous reporting reads. Keep the actual guarded
    # candidate snapshots and every numerical fit below, without copying the
    # in-memory database again for each uncontended reporting lookup.
    @contextmanager
    def reporting_view(**kwargs):
        with store._lock:
            yield store

    if (initial_targets, new_targets) == (26, 24):
        daily_catalog_template = request.getfixturevalue('daily_catalog_template')
        value = daily_catalog_template.restore(store)
    else:
        value = _seed_catalog(store)
    now, account, keys, videos = (value[name] for name in ('now', 'account', 'keys', 'videos'))
    if (initial_targets, new_targets) == (26, 24):
        file_backed_store = request.getfixturevalue('file_backed_store')
        sibling = daily_catalog_template.restore(file_backed_store)
        with prepared_template(_pristine_current_schema, _seed_catalog) as fresh:
            producer = fresh.source
            assert fresh.result == value == sibling
            for current in (daily_catalog_template.source, store, file_backed_store, producer):
                assert list(map(tuple, current.conn.execute(
                    'SELECT video_id,title,artist,created_at FROM tracks ORDER BY video_id'))) == list(map(tuple,
                    producer.conn.execute('SELECT video_id,title,artist,created_at FROM tracks ORDER BY video_id')))
                for offset in (0, 80):
                    playlist = current.playlists.get_playlist_for_account(account, f'PL{offset}')
                    assert len(current.playlists.get_playlist_track_ids(playlist.id)) == 80
                    assert current.playlists.get_playlist_track_keys(playlist.id) == set(keys[offset:offset + 80])
                assert list(map(tuple, current.conn.execute(
                    'SELECT identity_key,video_id,played_at FROM play_event_subjects ORDER BY id'))) == [
                        (key, videos[key], -100 * daily.DAY) for key in keys[:20]]
                assert current.rec_model.get_rec_vectors() == []
                assert current.model_selection.latest_daily_selection() is None
                assert current.conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert len({daily_catalog_template.source._evidence_key, store._evidence_key,
                    file_backed_store._evidence_key}) == 3
        for name, source in daily_catalog_template.files.items():
            destinations = [roots(target.conn)[name] for target in (store, file_backed_store)]
            assert len({source, *destinations}) == 3
            for path in source.rglob('*'):
                if path.is_file():
                    paths = [path, *(directory / path.relative_to(source) for directory in destinations)]
                    assert all(copied.read_bytes() == path.read_bytes() for copied in paths)
                    assert len({(copied.stat().st_dev, copied.stat().st_ino) for copied in paths}) == 3
        sibling['keys'].clear()
        sibling['videos'].clear()
        file_backed_store.settings.set_setting('private_catalog', 'sibling')
        assert len(keys) == len(videos) == len(daily_catalog_template.result['keys']) == 160
        assert len(daily_catalog_template.result['videos']) == 160
        assert store.settings.get_setting('private_catalog') is None
        assert daily_catalog_template.source.settings.get_setting('private_catalog') is None
    monkeypatch.setattr(store, 'read_view', reporting_view)
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


def test_file_database_reopen_resumes_completed_candidate(file_backed_store, monkeypatch):
    # Reopen the real installed database together with its external vectors.
    # A SQL-only backup of the memory fixture leaves those files behind.
    library = configured_library(file_backed_store, monkeypatch,
                                 _seed_library(file_backed_store, _seed_catalog(file_backed_store)))
    path = file_backed_store._db_path
    file_backed_store.conn.close()
    first = Store(path)
    first.conn.execute('PRAGMA synchronous=OFF')
    with schema_inspection.reuse_tables(first.conn):
        first.init_schema()
    try:
        run = daily.maintain(first, library['now'])
        assert len(run['_evaluations']) == 1
    finally:
        first.conn.close()
    reopened = Store(path)
    reopened.conn.execute('PRAGMA synchronous=OFF')
    with schema_inspection.reuse_tables(reopened.conn):
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


def test_failure_after_selection_publication_keeps_selected_vectors_and_restore_point(library, monkeypatch):
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
    assert daily.maintain(store, now + 10)['status'] == 'applied'
    assert published_rows == [len(library['keys'])]
    selected = store.rec_model.get_rec_vectors()
    assert len(selected) == len(library['keys'])
    assert selected != library['original']
    assert store.settings.get_setting('rec_dim') == '3'
    active = int(store.settings.get_setting('rec_selection_active_run'))
    assert dict(store.conn.execute('SELECT identity_key,vec FROM rec_model_previous_vectors WHERE run_id=?',
                                   (active,))) == dict(library['original'])


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


@pytest.mark.parametrize('phase', ['preparing', 'observing'])
def test_legacy_gate_ends_but_held_projections_survive(library, phase):
    from yt_playlist.rec import product_comparison
    from yt_playlist.rec import choice_evidence
    store, now = library['store'], library['now']
    setup = {'method': 'svd', 'dim': 2}
    old = dict(kind='product', status='running', phase='observing', ran_at=now - daily.DAY,
               previous=setup, selected=setup, grid=[])
    run_id = store.model_selection.start_selection(old['ran_at'], old)
    current = dict(run_id=run_id, phase=phase, seed='retained-compatibility',
        started_at=now - daily.DAY, exposure_until=now + daily.DAY,
        constraints=product_comparison.constraints(store), generation='retained-generation',
        policy=product_comparison.POLICY)
    store.rec_proposals.put_proposals(product_comparison.SURFACE, current, now)
    ledger = store.rec_ledger.evidence()
    artifact = ledger.artifact({'retained': 'original comparison inputs'})
    saved, assignments = {}, {}
    for format in product_comparison.FORMATS:
        for arm in ('incumbent', 'challenger'):
            surface = product_comparison.pair_surface(run_id, arm, format)
            saved[surface] = {'run_id': run_id, 'data': {
                'retained': [arm, format], 'context': {'artifact': artifact}}}
            store.rec_proposals.put_proposals(surface, saved[surface], now)
        # Held assignments came from an observing trial before its retirement.
        observing = {**current, 'phase': 'observing'}
        store.rec_proposals.put_proposals(product_comparison.SURFACE, observing, now)
        assignments[format] = product_comparison.assignment(store, format, 'held', now)
    store.rec_proposals.put_proposals(product_comparison.SURFACE, current, now)
    monitor = {'run_id': run_id, 'complete': False}
    store.rec_proposals.put_proposals(product_comparison.MONITOR, monitor, now)
    opportunity = choice_evidence.issue(store, 'mixes', [{'id': 'retained-option'}], now,
        context={'comparison': assignments['mixes'], 'retained': {'artifact': artifact}})
    assert choice_evidence.respond(store, opportunity, 'visible', now + 1, visible=['retained-option'])
    original_offer = ledger.get('opportunity', opportunity)
    original_artifact = ledger.get('artifact', artifact)
    store.settings.set_setting('rec_selection_live_run', str(run_id))
    daily.maintain(store, now)
    assert store.model_selection.selection_run(run_id)['status'] == 'retained'
    assert product_comparison.state(store)['phase'] == 'retained'
    assert product_comparison.assignment(store, 'mixes', 'new', now) is None
    assert all(store.rec_proposals.get_proposals(surface) == value for surface, value in saved.items())
    for format, assignment in assignments.items():
        surface = product_comparison.pair_surface(run_id, assignment['arm'], format)
        assert product_comparison.prepared(store, format, 'held', now + 2,
            existing=assignment) == (saved[surface]['data'], assignment)
    assert ledger.get('opportunity', opportunity) == original_offer
    assert ledger.get('artifact', artifact) == original_artifact
    assert store.rec_proposals.get_proposals(product_comparison.MONITOR) == {**monitor, 'complete': True}
    assert not store.settings.get_setting('rec_selection_live_run')
