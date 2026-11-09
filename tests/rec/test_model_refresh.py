"""Background execution preserves the established models and last-good serving."""
import asyncio
from tests.enrichment_support import seed_audio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from functools import wraps
from html.parser import HTMLParser
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import (account_growth, artist_model, embedding_training as embed,
                             model_refresh, mode_surfaces, rec_params, taste_modes)
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from tests.home_fixture_support import (home_routes_only, publish_menu, returning_listener,
                                        without_metadata_providers)
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, file_consumer_store as store,
    uncontended_file_consumer_store as rollback_store,
    native_sql_calls, reuse_reporting_reader, without_query_timing,
)


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock'), ('house', 'House'))) as schema:
        yield schema


def seed(store, *, distinct_albums=False, track_count=96, mode_count=3):
    # Keep the original 96 subjects, three genres, eight overlapping baskets
    # and audio. Catalog ingestion is below model publication and invalidation.
    items = [dict(key=f'song {i:03}|artist {i}', video_id=f'v{i}', title=f'Song {i:03}',
                  artist=f'Artist {i}', album=f'Album {i}' if distinct_albums else 'Album',
                  genre=('Techno', 'Jazz', 'Rock')[i * 3 // track_count]) for i in range(track_count)]
    seed_publication_subjects(store, items)
    with atomic_write(store):
        iid = store.accounts.upsert_account('Listener', 'fixture', None, True)
        tids = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
        first_artist = store.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM artists').fetchone()[0]
        credits = [(first_artist + i, tid, item['artist']) for i, (tid, item) in enumerate(zip(tids, items))]
        # Models consume completed artist/catalog inputs. Provider captures and
        # field reconciliation are exercised by their own owners.
        store.conn.executemany('INSERT INTO artists(id,name,provisional) VALUES (?,?,1)',
                               [(artist, name) for artist, _, name in credits])
        store.conn.executemany("INSERT INTO artist_aliases VALUES (?,?,'fixture')",
                               [(artist, name.lower()) for artist, _, name in credits])
        store.conn.executemany("INSERT INTO entry_artists VALUES (?,0,?,?,'fixture')",
                               [(tid, artist, name) for artist, tid, name in credits])
        store.conn.executemany('UPDATE recordings SET bpm=?,energy=? WHERE id=?',
                               [(80 + i, (i % 10) / 10, tid) for i, tid in enumerate(tids)])
        for i in range(8):
            size, offset = track_count // 4, i * (track_count // 8)
            pid = store.playlists.upsert_playlist(iid, f'p{i}', f'Playlist {i}', size, '', 1)
            store.playlists.set_playlist_tracks(pid, (tids + tids)[offset:offset + size])
        store.settings.set_setting('rec_dim', '16')
        rec_params.set_param(store, 'modes_k', mode_count)
        rec_params.set_param(store, 'modes_min_members', 8)
    # History's controller coordinates its own repository transactions after
    # the completed catalog batch has committed.
    store.history.import_play_events(iid, [(items[i]['key'], f'v{i}', 1000 + i)
                                         for i in range(8)])
    return tids


@pytest.fixture(scope='module')
def model_catalog_template(_pristine_current_schema):
    with ExitStack() as resources:
        with native_model_input_sql():
            template = resources.enter_context(prepared_template(_pristine_current_schema, seed))
        assert template.source.recommendations.head('home') is None
        assert template.source.rec_model.get_rec_vectors() == []
        yield template


@pytest.fixture(scope='module')
def initial_generation_template(_pristine_current_schema):
    def build(store):
        tids = seed(store)
        assert model_refresh.rebuild(store, 2000) is not None
        assert store.recommendations.head('home') is None
        return tids

    with ExitStack() as resources:
        with native_model_input_sql():
            template = resources.enter_context(prepared_template(_pristine_current_schema, build))
        yield template


@contextmanager
def native_model_input_sql():
    """Build completed model inputs below query diagnostics and handle admission."""
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.core.db_handles import Connection

    with pytest.MonkeyPatch.context() as patch:
        for operation in ('execute', 'executemany'):
            patch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))
        patch.setattr(DiagnosticConnection, 'cursor', Connection.cursor)
        yield


@pytest.fixture
def model_library(store, model_catalog_template):
    return model_catalog_template.restore(store)


@pytest.fixture
def initial_model_store(tmp_path, initial_generation_template, monkeypatch):
    with initial_generation_template.open(tmp_path / 'models.db') as database:
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture(autouse=True)
def refresh_costs(monkeypatch, record_property):
    totals, lock = {}, threading.Lock()
    for owner, name in (
        (model_refresh, 'rebuild'), (model_refresh, 'prepare'),
        (embed, 'build_vectors_from_baskets'), (taste_modes, 'prepare_modes'),
        (mode_surfaces, 'prepare_bundles'), (mode_surfaces, '_calculate_bundles'),
        (mode_surfaces, '_publish_typed_bundles'),
    ):
        original = getattr(owner, name)

        def timed(original=original, name=name):
            @wraps(original)
            def call(*args, **kwargs):
                started, cpu = time.perf_counter(), time.thread_time()
                try:
                    return original(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - started
                    used = time.thread_time() - cpu
                    with lock:
                        values = totals.setdefault(name, [0, 0.0, 0.0])
                        values[0] += 1
                        values[1] += elapsed
                        values[2] += used
            return call

        monkeypatch.setattr(owner, name, timed())
    yield
    # Inclusive phase totals overlap; thread CPU excludes a blocked worker's wait.
    for name, (calls, wall, cpu) in sorted(totals.items()):
        record_property(name + '_calls', calls)
        record_property(name + '_wall_s', wall)
        record_property(name + '_thread_cpu_s', cpu)


def test_unchanged_inputs_reuse_fits_but_metadata_and_baskets_invalidate_their_models(store, model_library, monkeypatch):
    tids = model_library
    assert model_refresh.rebuild(store, 2000)
    assert model_refresh.rebuild(store, 2001)['vectors'] == {}
    store.tracks.set_track_genre(tids[0], 'Classical')
    prepared = model_refresh.rebuild(store, 2002)
    assert 'rec_content_vectors' in prepared['vectors']
    assert 'rec_vectors' in prepared['vectors']  # genre baskets are collaborative inputs too
    assert not store.rec_refresh.has_pending_changes()


@pytest.mark.parametrize('change', [
    'genre', 'year', 'audio', 'membership', 'empty-library', 'missing-model',
    'genre-revision', 'missing-revision',
])
def test_production_refresh_invalidates_content_without_a_coverage_gate(store, monkeypatch, change):
    from yt_playlist.util import genre_catalog

    tid = store.tracks.upsert_track('one', 'One', 'Artist', None, None)
    store.tracks.set_track_genre(tid, 'Techno')
    assert model_refresh.rebuild(store, 1000)
    before = store.rec_model.content_generation()
    assert model_refresh.rebuild(store, 1001)['vectors'] == {}
    if change == 'genre':
        store.tracks.set_track_genre(tid, 'Folk')
    elif change == 'year':
        store.tracks.set_track_year(tid, '1995')
    elif change == 'audio':
        seed_audio(store, tid, bpm=120)
    elif change == 'membership':
        store.tracks.upsert_track('two', 'Two', 'Artist', None, None)
    elif change == 'empty-library':
        with store.conn:
            store.conn.execute('DELETE FROM library_memberships')
    elif change == 'missing-model':
        store.settings.delete_setting('rec_content_model')
    elif change == 'genre-revision':
        monkeypatch.setattr(genre_catalog, 'REVISION', 'next-catalog')
    elif change == 'missing-revision':
        store.settings.delete_setting('rec_content_genre_revision')
    prepared = model_refresh.rebuild(store, 1002)
    assert 'rec_content_vectors' in prepared['vectors']
    after = store.rec_model.content_generation()
    assert after['generation'] != before['generation']
    assert after['revision'] == genre_catalog.REVISION
    if change == 'empty-library':
        assert after['library'] == []
    assert model_refresh.rebuild(store, 1003)['vectors'] == {}


@pytest.mark.parametrize('external', [False, True])
def test_changed_source_keeps_last_good_generation_and_notification(
        initial_model_store, initial_generation_template, monkeypatch, external):
    store = initial_model_store
    tids = initial_generation_template.result
    before = deepcopy(store.rec_model.content_generation())
    store.tracks.set_track_genre(tids[0], 'Classical')
    original = model_refresh.prepare

    def changed(reader, now, **kwargs):
        prepared = original(reader, now, **kwargs)
        if external:
            writer = Store(store._db_path)
            try:
                writer.init_schema()
                assert writer.conn is not store.conn
                writer.tracks.set_track_genre(tids[1], 'Ambient')
                assert writer.tracks.get_track_enrichment(tids[1])[0] == 'Ambient'
            finally:
                writer.conn.close()
        else:
            store.tracks.set_track_genre(tids[1], 'Ambient')
        return prepared

    monkeypatch.setattr(model_refresh, 'prepare', changed)
    assert model_refresh.rebuild(store, 2001) is None
    assert store.tracks.get_track_enrichment(tids[1])[0] == 'Ambient'
    assert store.rec_model.content_generation() == before
    assert store.rec_refresh.has_pending_changes()
    assert initial_generation_template.source.rec_model.content_generation() == before
    assert initial_generation_template.source.tracks.get_track_enrichment(tids[1])[0] == 'Techno'


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_enrichment_stamps_allow_full_model_and_pool_publication(store, monkeypatch):
    seed(store)
    original_prepare = model_refresh.prepare

    def stamp():
        with store.conn:
            store.conn.execute('UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,1),'
                               'last_enriched_at=COALESCE(last_enriched_at,0)+1')

    def fitting(reader, now, **kwargs):
        result = original_prepare(reader, now, **kwargs)
        stamp()
        return result

    def ranking(reader, now, policies):
        # Scoring is below this stamp/publication race. Keep every original
        # subject in a completed bucket while the real snapshot is open.
        keys = sorted(reader.rec_query.library_keys())
        tracks = reader.rec_query.tracks_by_keys(keys)
        mode = str(reader.modes.list_modes(active_only=True)[0]['mode_id'])
        items = policies.stamp([dict(tracks[key], key=key) for key in keys], 'wheelhouse')
        stamp()
        return {mode: {'wheelhouse': items}, 'all': {'wheelhouse': items},
                '_meta': {'full_pools': True}}, []

    monkeypatch.setattr(model_refresh, 'prepare', fitting)
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', ranking)
    assert model_refresh.rebuild(store, 2000) is not None
    assert not store.rec_refresh.has_pending_changes()
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert store.recommendations.head('home') is not None


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_failed_publication_rolls_back_models_settings_clusters_and_notification(
        rollback_store, model_catalog_template, monkeypatch):
    from yt_playlist.core import embedding_files, embeddings, fao, model_contract

    # Files and failed SQL publication stay real; snapshot coordination is
    # uncontended below this rollback-and-retry workflow.
    store = rollback_store
    tids = seed(store)
    assert tids == model_catalog_template.result
    for getter in ('library_keys', 'track_content', 'track_audio_features', 'play_counts'):
        assert getattr(store.rec_query, getter)() == getattr(model_catalog_template.source.rec_query, getter)()
    assert model_refresh.rebuild(store, 2000)
    assert not store.rec_refresh.has_pending_changes()
    before = store.rec_model.content_generation()
    assert before['coherent'] and before['generation'] and before['library']
    modes = [(m['mode_id'], m['centroid'].tobytes()) for m in store.modes.list_modes()]
    assert modes
    vectors = {name: embeddings.rows(store.conn, name) for name in embeddings.SPACES}
    assert all(rows for name, rows in vectors.items() if name != 'rec_discovered_content_vectors')
    settings = {key: store.settings.get_setting(key) for key in (
        *model_contract.FIELDS, 'rec_refresh_version', 'rec_mode_fit_inputs', 'rec_embed_method', 'rec_dim')}
    store.tracks.set_track_genre(tids[0], 'Classical')
    revision = store.rec_refresh.revision()
    assert revision is not None
    prepared_batches, prepare = [], model_refresh.prepare

    def capture_batch(*args, **kwargs):
        batch = prepare(*args, **kwargs)
        prepared_batches.append(deepcopy(batch))
        return batch

    monkeypatch.setattr(model_refresh, 'prepare', capture_batch)
    writes = []
    original_write = fao.write
    def write_without_serving_lock(path, data, **kwargs):
        if path.suffix == '.bin':
            assert not store._lock.held_by_current_thread, 'Embedding bytes prepared under the serving lock'
            assert not store.conn.in_transaction
            writes.append(path)
        return original_write(path, data, **kwargs)
    monkeypatch.setattr(fao, 'write', write_without_serving_lock)

    def selected():
        return {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
                for table in ('recording_embedding_files', 'artist_embedding_files',
                              'taste_model_state', 'taste_modes', 'rec_change_queue')}

    def recording_references():
        return [tuple(row) for row in store.conn.execute('SELECT * FROM recording_embedding_files ORDER BY 1,2')]

    recording_before = recording_references()
    embedding_paths = set(embedding_files.references(store.conn))
    selected_before = selected()
    model_files = [row[0] for row in store.conn.execute(
        'SELECT recovery_filename FROM taste_model_state WHERE recovery_filename IS NOT NULL')]
    file_bytes = {path: fao.read(path).bytes for path in (*embedding_paths, *model_files)}
    reached = []

    def publication_started():
        reached.append((store.conn.in_transaction, recording_references(),
                        {path: fao.read(path).bytes for path in embedding_files.references(store.conn)}))
        return 0

    store.conn.create_function('publication_started', 0, publication_started)
    try:
        store.conn.execute("CREATE TRIGGER fail_training BEFORE INSERT ON artist_embedding_files "
                           "WHEN NEW.space='content' BEGIN SELECT publication_started(); "
                           "SELECT RAISE(ABORT,'injected failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
            model_refresh.rebuild(store, 2001)
        assert writes
        assert len(reached) == 1
        in_transaction, changed_references, written_chunks = reached[0]
        assert in_transaction and changed_references != recording_before
        assert written_chunks.keys() - embedding_paths  # real new chunks were selected inside the failed write
        assert not store.conn.in_transaction
        store.settings.set_setting('unrelated', 'commit after failure')
        assert selected() == selected_before
        assert {path: fao.read(path).bytes for path in file_bytes} == file_bytes
        assert set(embedding_files.references(store.conn)) == set(file_bytes) - set(model_files)
        assert {name: embeddings.rows(store.conn, name) for name in embeddings.SPACES} == vectors
        assert {key: store.settings.get_setting(key) for key in settings} == settings
        assert store.rec_model.content_generation() == before
        assert [(m['mode_id'], m['centroid'].tobytes()) for m in store.modes.list_modes()] == modes
        assert store.rec_refresh.revision() == revision and store.rec_refresh.has_pending_changes()
    finally:
        store.conn.execute('DROP TRIGGER IF EXISTS fail_training')
        store.conn.create_function('publication_started', 0, None)

    # Retry publication of the same completed fit. Numerical fitting has its
    # own parity tests; the snapshot, file writes and repository writes remain
    # real on both attempts.
    assert len(prepared_batches) == 1
    monkeypatch.setattr(model_refresh, 'prepare', lambda *args, **kwargs: deepcopy(prepared_batches[0]))
    retry = model_refresh.rebuild(store, 2002)
    assert retry is not None
    after = store.rec_model.content_generation()
    assert after['coherent'] and after['generation'] != before['generation']
    for name, rows in retry['vectors'].items():
        assert dict(embeddings.rows(store.conn, name)) == dict(rows)
    assert {key: store.settings.get_setting(key) for key in retry['settings']} == retry['settings']
    assert store.settings.get_setting('unrelated') == 'commit after failure'
    assert not store.rec_refresh.has_pending_changes()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_coalesced_notifications_survive_restart_and_do_not_acknowledge_later_changes(
        file_backed_store, model_catalog_template):
    store = file_backed_store
    path = store._db_path
    model_catalog_template.restore(store)
    assert store.conn.execute('SELECT COUNT(*) FROM rec_change_queue').fetchone()[0] == 1
    revision = store.rec_refresh.revision()
    store.rec_refresh.enqueue()
    store.rec_refresh.acknowledge(revision)
    assert store.rec_refresh.has_pending_changes()
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        assert store.rec_refresh.has_pending_changes()
        assert model_refresh.rebuild(store, 2000)
        assert not store.rec_refresh.has_pending_changes()
    finally:
        store.conn.close()


def test_background_fit_leaves_event_loop_and_last_good_home_available(
        file_backed_store, monkeypatch, home_routes_only, without_metadata_providers):
    store = file_backed_store
    # This case owns worker/request concurrency and last-good serving. Ranked
    # menu/model equivalence is checked above. Four playable cards need one
    # subject each here; ranking additional subjects adds no concurrency state.
    prepared = publish_menu(store, 2000, tracks_per_card=1)
    tids = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
    iid = store.accounts.upsert_account('Listener', 'fixture', None, True)
    pid = store.playlists.upsert_playlist(iid, 'first', 'First', len(tids), '', 1)
    store.playlists.set_playlist_tracks(pid, tids)
    store.settings.set_setting('rec_dim', '4')
    # Numerical equivalence belongs to the two real fitting owners above. Keep
    # capture, invalidation, worker dispatch and SQL/file model publication real.
    def completed_fit(baskets, *, dim, method, weights):
        keys = sorted({key for basket in baskets for key in basket})
        matrix = np.zeros((len(keys), dim), dtype=np.float32)
        matrix[:, 0] = 1.
        return keys, matrix

    monkeypatch.setattr(embed, 'build_vectors_from_baskets', completed_fit)
    # This completed library has no out-of-corpus artist profiles. Discovery's
    # independent producer and its model parity are not part of worker dispatch.
    monkeypatch.setattr(artist_model, '_discovered_artist_profiles', lambda reader: {})
    assert model_refresh.rebuild(store, 2000) is not None
    assert store.rec_model.rec_vectors_count() > 0
    # Change an actual collaborative input so the worker must enter its fitter.
    pid = store.playlists.upsert_playlist(iid, 'second', 'Second', 2, '', 1)
    store.playlists.set_playlist_tracks(pid, tids[:2])
    # Completed listening evidence is an input to this concurrency contract.
    # Choice compilation has its own owners in test_choice_evidence; keep the
    # actual persisted report and access check without rerunning that producer.
    returning_listener(store, 'mixes', now=1993)
    assert store.rec_proposals.choice_usage()['episodes']['mixes'] == 1
    assert any(len(menu['protos']) == 4 for menu in prepared['menus']), [
        [proto['lane'] for proto in menu['protos']] for menu in prepared['menus']]
    expected_cards = {
        f"gen-{proto['lane']}": [(track['key'], track['video_id']) for track in proto['tracks']]
        for proto in prepared['menus'][0]['protos']}
    assert account_growth.has_access(account_growth.view(store, 2000), 'directions')
    app = create_app(store, lambda: {}, now_fn=lambda: 2000, background_work=False)
    ctx = SimpleNamespace(store=store, work=Activity(), now_fn=lambda: 2000,
                          logger=logging.getLogger(__name__))
    worker = RecWorker(ctx, debounce_s=0)
    # Incremental content-basis preparation is independent of this complete-fit
    # concurrency contract (test_progressive). Keep it idle so the generation
    # observed here belongs to the full fitter being deliberately blocked.
    monkeypatch.setattr(worker.preparation, 'capture', lambda now: False)
    monkeypatch.setattr(worker.preparation, 'fit', lambda now: False)
    started, release = threading.Event(), threading.Event()
    original = embed.build_vectors_from_baskets
    main_thread = threading.get_ident()
    def slow_fit(*args, **kwargs):
        assert threading.get_ident() != main_thread
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', slow_fit)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            def playable_cards(response):
                assert response.status_code == 200, response.text
                class Cards(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.cards, self.launchable, self.current = {}, set(), None

                    def handle_starttag(self, tag, attributes):
                        attrs = dict(attributes)
                        classes = (attrs.get('class') or '').split()
                        if tag == 'section' and 'gen-card' in classes:
                            self.current = attrs['id']
                            self.cards[self.current] = []
                        elif self.current and tag == 'li' and 'gen-row' in classes:
                            self.cards[self.current].append((attrs['data-key'], attrs['data-vid']))
                        elif self.current and tag == 'button' and attrs.get('hx-post') == '/home/generate':
                            self.launchable.add(self.current)

                document = Cards()
                document.feed(response.text)
                assert len(document.cards) == 4, response.text
                assert document.launchable == document.cards.keys(), response.text
                for rows in document.cards.values():
                    assert rows and all(key and video for key, video in rows)
                return document.cards

            generation = store.rec_model.content_generation()
            worker.trigger()
            assert await asyncio.to_thread(started.wait, 5)
            # One request made after the fitter is blocked proves this overlap.
            # Repeating the identical render adds no distinct state transition.
            response = await asyncio.wait_for(client.get('/home/cards'), 1)
            assert playable_cards(response) == expected_cards
            assert store.rec_model.content_generation() == generation
            assert await asyncio.wait_for(asyncio.to_thread(store.rec_model.rec_vectors_count), 1) > 0
        assert not release.is_set()
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        worker.shutdown()
        worker.join(5)
        store.conn.close()


def test_cancelled_fit_never_publishes_partial_models(store, model_library, monkeypatch):
    before = store.rec_model.get_rec_vectors()
    def cancelled(*args, **kwargs):
        raise WorkClosing('stopped')
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', cancelled)
    with pytest.raises(WorkClosing):
        model_refresh.rebuild(store, 2000)
    assert store.rec_model.get_rec_vectors() == before
    assert store.rec_refresh.has_pending_changes()
