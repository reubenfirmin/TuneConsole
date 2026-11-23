"""Each foreground consumer stays usable during real installed inventory work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.home_fixture_support import returning_listener, service
from tests.onboarding_support import publish_live_mix_inputs
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.rec.test_evidence_responsiveness import (
    assert_maintenance_banner, install_serving_routes, seed_evidence_library, serving_bytecode)
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import group_evidence, home_outputs, recommendation_impressions
from yt_playlist.core.group_preparation_state import ROOTS
from yt_playlist.core.worker_settings import number, save
from yt_playlist.rec import choice_evidence, group_root_backfill, group_roots, home_cards, rec_params
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.web.app import create_app


def pending_inventory(store, identity):
    captured = {'at': 10, 'group_generation': 'first', 'musical_groups': [
        {'group_id': 'stable-parent', 'version': 'first-parent'},
        {'group_id': 'stable-leaf', 'version': 'first-leaf', 'parent_version': 'first-parent'}]}
    ledger = store.rec_ledger.evidence()
    artifact = ledger.artifact(captured)
    ledger.put('opportunity', identity, {'id': identity, 'schema': 1, 'format': 'mixes',
        'at': 10, 'observed_at': 10, 'context': artifact, 'options': [{'id': 'one'}]})
    store.rec_proposals.put_proposals('album_prepared', {
        'context': {'artifact': artifact}, 'items': [], 'built_at': 10}, 10)
    publication = home_outputs.manifest(store.conn, 'album_prepared')
    assert publication is not None
    owner = f'home-output:{publication.publication_id}:context'
    assert store.group_evidence.has_owner('prepared', owner)
    with group_roots.writer(store) as conn:
        save(conn, ROOTS, 0)
    store._group_root_backfill = group_root_backfill.Backfill(store)
    return identity, owner


def assert_prepared_roots(store, owner):
    assert [row[0] for row in store.conn.execute(
        f'SELECT reference.value->>0 FROM {group_evidence.HOME_VERSIONS} '
        'AND context.value->>2=? ORDER BY 1', (owner,))] == ['first-leaf', 'first-parent']
    assert store.conn.execute(
        "SELECT 1 FROM group_evidence_contexts e JOIN group_generations g ON g.id=e.generation_id "
        "WHERE e.kind='prepared' AND e.external_key=? AND g.external_key='first'", (owner,)).fetchone()


def assert_live_mixes(response):
    assert response.status_code == 200
    for lane in ('comfort', 'wheelhouse', 'explore', 'fresh'):
        assert f'id="gen-{lane}"' in response.text
        assert f'data-vid="live-{lane}-' in response.text


@pytest.fixture(scope='module')
def inventory_admitted_input(tmp_path_factory):
    path = tmp_path_factory.mktemp('inventory-current') / 'inventory.db'
    entries = (('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz'))
    with bounded_consumer_schema(entries) as schema, prepared_template(
            schema, lambda store: None, database=path) as template:
        yield template


def test_inventory_input_has_real_revision_guards(inventory_admitted_input):
    source = inventory_admitted_input.source
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert inventory_admitted_input.revision_guards


@pytest.fixture(scope='module')
def inventory_evidence_input(inventory_admitted_input, tmp_path_factory):
    path = tmp_path_factory.mktemp('inventory-evidence') / 'evidence.db'
    with inventory_admitted_input.open(path) as store:
        seed_evidence_library(store)
        yield capture_template(store, None)


def test_inventory_inputs_publish_both_real_group_versions_and_retained_choice_evidence(inventory_evidence_input):
    store = inventory_evidence_input.source
    assert store.taste_groups.head()['generation'] == 'first'
    for version in ('first-parent', 'first-leaf'):
        assert store.taste_groups.definition(version, generation='first')
    opportunities = store.rec_ledger.evidence().records('opportunity')
    assert len(opportunities) == 1 and opportunities[0]['options']
    assert len(store.rec_query.library_keys()) == 2


@pytest.fixture(scope='module')
def inventory_serving_input(inventory_evidence_input, tmp_path_factory):
    path = tmp_path_factory.mktemp('inventory-serving') / 'serving.db'
    with inventory_evidence_input.open(path) as store:
        publish_live_mix_inputs(store, tracks_per_lane=8)
        rec_params.set_param(store, 'comfort_min_pool', 0)
        store.accounts.upsert_account('Main', None, True)
        store.settings.set_setting('last_sync_at', '1000')
        store.settings.set_setting('onboard_dismissed', '1')
        returning_listener(store, 'mixes', now=1000)
        yield capture_template(store, None)


def test_inventory_serving_inputs_retain_ranked_candidates_and_a_real_typed_head(inventory_serving_input):
    store = inventory_serving_input.source
    assert store.recommendations.head('home')
    assert store.recommendations.head('fresh')
    # Completed serving output is usable before historical inventory finishes.
    assert number(store.conn, ROOTS) == 0


def test_inventory_home_serves_every_lane_before_recovery(cached_inventory_app):
    # Own the ordinary Home request and its first real template compile;
    # held preparation below then isolates each concurrent foreground request.
    library, client, _ = cached_inventory_app
    response = client.get('/')
    assert_live_mixes(response)
    assert_maintenance_banner(response)
    assert not library.activity_paused


@pytest.fixture(scope='module')
def cached_inventory_app(inventory_serving_input, tmp_path_factory, serving_bytecode):
    from yt_playlist.providers import genres

    path = tmp_path_factory.mktemp('cached-inventory-app') / 'cached-inventory.db'
    with inventory_serving_input.open(path) as library, pytest.MonkeyPatch.context() as patch:
        install_serving_routes(patch, serving_bytecode)
        patch.setattr(genres, 'configure', lambda *args, **kwargs: None)
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
        worker = app.state.ctx.rec_worker
        patch.setattr(worker, '_start_loop', lambda: None)
        client = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
        try:
            yield library, client, worker
        finally:
            worker.shutdown()
            client.close()


@pytest.fixture(scope='module')
def cached_inventory_recovery():
    return {}


def test_cached_home_does_not_wait_for_inventory_file_preparation(
        cached_inventory_app, cached_inventory_recovery, monkeypatch, tmp_path):
    library, client, _ = cached_inventory_app
    entered, release = threading.Event(), threading.Event()
    maintenance_thread = [None]
    observed = {'home_timed_out': False, 'cleanup_errors': []}
    try:
        # The preceding live-app check warmed Home before this gated file existed.
        identity, owner = pending_inventory(library, 'inventory-serving-check')
        ledger = library.rec_ledger.evidence()
        path = ledger._path('opportunity', identity)
        raw = path.read_bytes()
        assert not library.conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind='offer' "
                                        'AND external_key=?', (identity,)).fetchone()
        owners = {tuple(row) for row in library.conn.execute('SELECT * FROM group_evidence_contexts')}
        backfill = library._group_root_backfill
        record_reader = type(ledger)
        read_record = record_reader.read

        def held_read(reader, candidate):
            if candidate == path and threading.get_ident() == maintenance_thread[0] and not entered.is_set():
                # The exact cursor proves this is inventory preparation, rather
                # than a later acknowledgement or an unrelated Home file read.
                assert backfill.pending_path == path
                entered.set()
                assert release.wait(10), 'Inventory test did not release its controlled file read'
            return read_record(reader, candidate)

        def maintain():
            maintenance_thread[0] = threading.get_ident()
            return choice_evidence.maintain_storage(library, 1000)

        monkeypatch.setattr(record_reader, 'read', held_read)
        request = None
        with ThreadPoolExecutor(2) as executor:
            maintenance = executor.submit(maintain)
            try:
                assert entered.wait(2), 'Maintenance did not reach the controlled inventory file'
                assert not maintenance.done() and not release.is_set()
                started = time.monotonic()
                request = executor.submit(client.get, '/')
                try:
                    home = request.result(timeout=2)
                except TimeoutError:
                    observed['home_timed_out'] = True
                else:
                    assert_live_mixes(home)
                    assert_maintenance_banner(home)
                    assert not maintenance.done() and not release.is_set()
                observed['home_elapsed_before_release'] = time.monotonic() - started
                observed['maintenance_held_before_release'] = not maintenance.done()
            finally:
                # A failed foreground deadline must never strand the writer or
                # let either thread outlive this fixture's database connection.
                release.set()
                for name, future in (('maintenance', maintenance), ('home', request)):
                    if future is not None:
                        try:
                            future.result(timeout=10)
                        except Exception as error:
                            observed['cleanup_errors'].append(f'{name}: {type(error).__name__}: {error}')
        monkeypatch.setattr(record_reader, 'read', read_record)
        cached_inventory_recovery.update(
            identity=identity, owner=owner, path=path, raw=raw, owners=owners, backfill=backfill)
        assert not observed['home_timed_out'], (
            'Cached Home waited more than 2 seconds for held inventory file preparation; '
            'see inventory-serving.json')
        assert not observed['cleanup_errors'], observed
    finally:
        release.set()
        (tmp_path / 'inventory-serving.json').write_text(json.dumps(observed, indent=2))


def test_cached_inventory_recovery_completes_its_cursor_without_rewriting_files_or_losing_owners(
        cached_inventory_app, cached_inventory_recovery):
    library, _, _ = cached_inventory_app
    state = cached_inventory_recovery
    assert state, 'The held-read foreground check must establish the historical inventory first'
    for _ in range(10):
        if not choice_evidence.maintain_storage(library, 1000).get('deferred'):
            break
    assert number(library.conn, ROOTS) == 1
    backfill = state['backfill']
    assert backfill.stage == len(group_root_backfill.KINDS) and backfill.pending_path is None
    assert state['path'].read_bytes() == state['raw']
    assert state['owners'] <= {tuple(row) for row in library.conn.execute('SELECT * FROM group_evidence_contexts')}
    assert not library.conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind IN ('offer','checkpoint') "
                                    'AND external_key=?', (state['identity'],)).fetchone()
    assert_prepared_roots(library, state['owner'])
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('shared_writer', [True, False], ids=['serving-writer', 'separate-writer'])
def test_home_onboarding_and_growth_do_not_wait_for_a_cleanup_writer(inventory_serving_input, monkeypatch, tmp_path, shared_writer):
    with inventory_serving_input.open(tmp_path / 'held-schema.db') as library:
        library.accounts.upsert_account('Main', None, True)
        library.settings.set_setting('last_sync_at', '1000')
        # The asserted mix dashboard belongs to an established listener.
        returning_listener(library, 'mixes', now=1000)
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=False)
        entered, release = threading.Event(), threading.Event()
        before = recommendation_impressions.counts(library.conn, recommendation_impressions.CARD)
        assert not library.activity_paused
        acquire = library._lock.acquire

        def foreground_acquire(blocking=True, timeout=-1):
            if entered.is_set() and not release.is_set() and not library._lock.held_by_current_thread:
                assert not blocking, 'Home tried to wait for the held cleanup writer'
            return acquire(blocking=blocking, timeout=timeout)

        monkeypatch.setattr(library._lock, 'acquire', foreground_acquire)

        def hold_writer():
            conn = library.conn if shared_writer else sqlite3.connect(library._db_path)
            try:
                with library._lock if shared_writer else nullcontext():
                    with conn:
                        conn.execute('BEGIN IMMEDIATE')
                        entered.set()
                        assert release.wait(10)
            finally:
                if not shared_writer:
                    conn.close()

        with TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True}) as client:
            with ThreadPoolExecutor(2) as executor:
                library._recommendation_retirement_pending = True
                writer = executor.submit(hold_writer)
                try:
                    assert entered.wait(2)
                    # The lock hook rejects every blocking serving-lock acquisition.
                    # Allow cold template compilation within the first-page deadline.
                    response = executor.submit(client.get, '/')
                    home = response.result(timeout=5)
                    assert_live_mixes(home)
                    assert_maintenance_banner(home, visible=True)
                    response = executor.submit(client.get, '/?panel=unlocks&fragment=true')
                    unlocks = response.result(timeout=5)
                    assert unlocks.status_code == 200
                    assert 'id="growth-title"' in unlocks.text
                    assert 'data-landmark="first"' in unlocks.text
                    assert not writer.done()
                finally:
                    release.set()
                    writer.result(timeout=5)
                    library._recommendation_retirement_pending = False
            assert recommendation_impressions.counts(library.conn, recommendation_impressions.CARD) == before
            assert library.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
            assert not library.activity_paused
            # The same running app resumes ordinary visits after admission is free.
            old_views = library.rec_ledger.card_views('comfort')
            assert client.get('/').status_code == 200
            assert library.rec_ledger.card_views('comfort') == old_views + 1


@pytest.mark.parametrize('endpoint', ['home', 'playlists', 'status', 'settings', 'assembly', 'radio', 'retry'])
def test_inventory_preparation_keeps_each_foreground_consumer_responsive(
        inventory_serving_input, tmp_path, monkeypatch, endpoint):
    with inventory_serving_input.open(tmp_path / 'inventory.db') as library:
        oid, owner = pending_inventory(library, 'inventory-home')
        path = library.rec_ledger.evidence()._path('opportunity', oid)
        original = path.read_bytes()
        entered, release = threading.Event(), threading.Event()
        record_reader = type(library.rec_ledger.evidence())
        read_record = record_reader.read

        def hold():
            if not entered.is_set():
                entered.set()
                assert release.wait(3), 'Foreground consumer waited for inventory preparation'

        def held_read(reader, candidate):
            if candidate == path:
                hold()
            return read_record(reader, candidate)

        monkeypatch.setattr(record_reader, 'read', held_read)
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
        worker = app.state.ctx.rec_worker
        monkeypatch.setattr(worker, '_start_loop', lambda: None)
        client = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
        try:
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(choice_evidence.maintain_storage, library, 1000)
                try:
                    assert entered.wait(2), 'Real inventory preparation did not reach its held phase'
                    started = time.monotonic()
                    if endpoint == 'home':
                        response = client.get('/')
                        assert_live_mixes(response)
                        assert_maintenance_banner(response)
                    elif endpoint == 'playlists':
                        response = client.get('/playlists')
                        assert response.status_code == 200 and 'playlistsTab(' in response.text
                    elif endpoint == 'status':
                        response = client.get('/storage-maintenance/status')
                        assert response.status_code == 200
                        status = response.json()
                        assert not status['active'] and status['evidence_maintenance']['running']
                    elif endpoint == 'settings':
                        response = client.post('/settings/lastfm-key', data={'key': 'during-inventory'})
                        assert response.status_code == 204
                        assert library.settings.get_setting('lastfm_api_key') == 'during-inventory'
                    elif endpoint == 'assembly':
                        with library.read_view() as reader:
                            result = service(reader).cards(home_cards.CardsRequest(1000, record=False))
                        assert {proto['lane'] for proto in result.protos if proto['tracks']} == {
                            'comfort', 'wheelhouse', 'explore', 'fresh'}
                    elif endpoint == 'radio':
                        radio = PreparedRadio(library, 1000)
                        try:
                            with library.read_view() as reader:
                                head = next((value for surface in ('radio', 'home', 'fresh')
                                    if (value := reader.recommendations.head(surface)) is not None), None)
                            assert head is not None
                            assert radio.generation == head['id']
                            assert radio._lifetime is not None
                        finally:
                            radio.close()
                    else:
                        assert choice_evidence.maintain_storage(library, 1000) == {'deferred': True}
                    assert time.monotonic() - started < 2
                    assert not future.done() and not release.is_set()
                    assert number(library.conn, ROOTS) == 0
                finally:
                    release.set()
                result = future.result(5)
            monkeypatch.setattr(record_reader, 'read', read_record)
            for _ in range(10):
                if not result.get('deferred'):
                    break
                result = choice_evidence.maintain_storage(library, 1000)
            assert not result.get('deferred')
            assert not library.conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind='offer' AND external_key=?",
                                            (oid,)).fetchone()
            assert_prepared_roots(library, owner)
            assert number(library.conn, ROOTS) == 1
            assert path.read_bytes() == original and not library.activity_paused
            assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            release.set()
            worker.shutdown()
            client.close()


def test_historical_recovery_resumes_new_choices_and_worker_measurement_flush(
        inventory_serving_input, tmp_path, monkeypatch):
    with inventory_serving_input.open(tmp_path / 'resumed-evidence.db') as library:
        pending_inventory(library, 'resumed-prepared-home')
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
        worker = app.state.ctx.rec_worker
        monkeypatch.setattr(worker, '_start_loop', lambda: None)
        client = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
        try:
            for _ in range(10):
                result = choice_evidence.maintain_storage(library, 1000)
                if not result.get('deferred'):
                    break
            assert not result.get('deferred')
            assert not client.get('/storage-maintenance/status').json()['active']
            oid = choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 1001, context={})
            assert oid and choice_evidence.respond(library, oid, 'choice', 1002, option_id='one')
            worker._flush_measurements()
            assert library.rec_ledger.evidence().get('response', oid + ':choice:one')
            assert library.taste_groups.definition('first-leaf', generation='first')
            assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            worker.shutdown()
            client.close()
