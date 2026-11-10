"""Each foreground consumer stays usable during real installed inventory work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack, nullcontext
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.file_content_support import patch_byte_reads, read_bytes
from tests.file_content_support import read_bytes as original_bytes
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import returning_listener, without_metadata_providers
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.rec.test_evidence_responsiveness import (
    assert_maintenance_banner, historical_inventory, publish_serving_inputs,
    install_serving_routes, seed_evidence_library, serving_bytecode, serving_routes)
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.migrations import regular, runner
from yt_playlist.rec import choice_evidence, home_cache, home_cards, home_prepared, group_root_backfill, group_roots, stored_home
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.web.app import create_app


@pytest.fixture(scope='module')
def inventory_numbered_input():
    with closing(sqlite3.connect(':memory:')) as schema:
        with pytest.MonkeyPatch.context() as patch:
            bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))
            install_numbered_input(schema, 94)
        schema.execute('PRAGMA query_only=ON')
        yield schema


def test_inventory_predecessor_retains_the_real_v94_numbered_contract(inventory_numbered_input):
    runner.validate_version(inventory_numbered_input, 94)
    assert inventory_numbered_input.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 94
    assert inventory_numbered_input.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0] == 0


@pytest.fixture(scope='module')
def inventory_admitted_input(inventory_numbered_input, tmp_path_factory):
    path = tmp_path_factory.mktemp('inventory-installed') / 'installed.db'
    with ExitStack() as resources:
        with pytest.MonkeyPatch.context() as patch:
            bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))
            template = resources.enter_context(prepared_template(
                inventory_numbered_input, lambda store: None, database=path, defer_metadata=True))
        yield template


def test_inventory_admission_keeps_history_and_installs_real_regular_guards(inventory_admitted_input):
    source = inventory_admitted_input.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, 94)
        regular.validate(reader)
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
        publish_serving_inputs(store, tracks_per_card=1)
        store.accounts.upsert_account('Main', 'bridge', None, True)
        store.settings.set_setting('last_sync_at', '1000')
        store.settings.set_setting('onboard_dismissed', '1')
        returning_listener(store, 'mixes', now=1000)
        yield capture_template(store, None)


def test_inventory_serving_inputs_retain_four_playable_cards_and_a_real_typed_head(inventory_serving_input):
    store = inventory_serving_input.source
    payload = home_cache.read(store, 'home_tuned')
    assert home_prepared._valid(payload)
    protos = payload['menus'][0]['protos']
    assert {proto['lane'] for proto in protos} == {'comfort', 'wheelhouse', 'explore', 'temporal'}
    assert all(len(proto['tracks']) == 1 for proto in protos)
    assert store.recommendations.head('home')
    # Completed serving output is usable before historical inventory finishes.
    assert store.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0] == 0


def test_inventory_cached_home_serves_every_lane_before_recovery(cached_inventory_app):
    # Own the ordinary cached-page contract and its first real template compile;
    # held preparation below then isolates each concurrent foreground request.
    library, client, _ = cached_inventory_app
    response = client.get('/')
    assert response.status_code == 200
    for lane in ('comfort', 'wheelhouse', 'explore', 'temporal'):
        assert f'A {lane} mix for you' in response.text
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
        identity, surface = historical_inventory(library, 'inventory-serving-check')
        ledger = library.rec_ledger.evidence()
        path = ledger._path('opportunity', identity)
        raw = path.read_bytes()
        assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='offer' "
                                        'AND external_key=?', (identity,)).fetchone()
        owners = {tuple(row) for row in library.conn.execute('SELECT * FROM group_evidence_owners')}
        # This disposable fixture represents an upgrade with a legacy file not
        # yet inventoried. Do not leave retention open over that missing owner.
        with group_roots.writer(library) as conn:
            conn.execute('UPDATE group_retention_state SET roots_complete=0 WHERE id=1')
        backfill = group_root_backfill.Backfill(library)
        library._group_root_backfill = backfill
        read_bytes = original_bytes

        def held_read(candidate, **kwargs):
            if candidate == path and threading.get_ident() == maintenance_thread[0] and not entered.is_set():
                # The exact cursor proves this is inventory preparation, rather
                # than a later acknowledgement or an unrelated Home file read.
                assert backfill.pending_path == path
                entered.set()
                assert release.wait(10), 'Inventory test did not release its controlled file read'
            return read_bytes(candidate, **kwargs)

        def maintain():
            maintenance_thread[0] = threading.get_ident()
            return choice_evidence.maintain_storage(library, 1000)

        patch_byte_reads(monkeypatch, held_read)
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
                    assert home.status_code == 200 and 'A comfort mix for you' in home.text
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
        patch_byte_reads(monkeypatch, read_bytes)
        cached_inventory_recovery.update(
            identity=identity, surface=surface, path=path, raw=raw, owners=owners, backfill=backfill)
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
    assert library.conn.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()[0] == 1
    backfill = state['backfill']
    assert backfill.stage == len(group_root_backfill.KINDS) and backfill.pending_path is None
    assert state['path'].read_bytes() == state['raw']
    assert state['owners'] <= {tuple(row) for row in library.conn.execute('SELECT * FROM group_evidence_owners')}
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind IN ('offer','checkpoint') "
                                    'AND external_key=?', (state['identity'],)).fetchone()
    owner = 'proposal:' + state['surface'] + ':context'
    assert [row[0] for row in library.conn.execute("SELECT external_version FROM group_evidence_versions "
        "WHERE kind='comparison' AND external_key=? ORDER BY external_version", (owner,))] == [
            'first-leaf', 'first-parent']
    assert library.conn.execute("SELECT 1 FROM group_evidence_generations e JOIN group_generations g "
        "ON g.id=e.generation_id WHERE e.kind='comparison' AND e.external_key=? AND g.external_key='first'",
        (owner,)).fetchone()
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('shared_writer', [True, False], ids=['serving-writer', 'separate-writer'])
def test_home_onboarding_and_growth_do_not_wait_for_a_schema_writer(inventory_serving_input, monkeypatch, tmp_path, shared_writer):
    from yt_playlist.util import genre_map

    with inventory_serving_input.open(tmp_path / 'held-schema.db') as library:
        library.accounts.upsert_account('Main', 'bridge', None, True)
        library.settings.set_setting('last_sync_at', '1000')
        prepared_home = library.rec_proposals.get_proposals('home_tuned')
        families = genre_map.all_families()
        prepared_home['fingerprint'] = {'breadth': {'families': {
            family: 1 / len(families) for family in families}, 'breadth': 1},
            'eras': [], 'transient': {}}
        library.rec_proposals.put_proposals('home_tuned', prepared_home, 1000)
        # The asserted mix dashboard belongs to an established listener. A complete
        # menu alone does not bypass the fresh-account journey's preceding stops.
        returning_listener(library, 'mixes', now=1000)
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=False)
        entered, release = threading.Event(), threading.Event()
        before = list(map(tuple, library.conn.execute('SELECT * FROM rec_impressions ORDER BY surface,item_key')))
        assert not library.activity_paused
        acquire = library._lock.acquire

        def foreground_acquire(blocking=True, timeout=-1):
            if entered.is_set() and not release.is_set() and not library._lock.held_by_current_thread:
                assert not blocking, 'Home tried to wait for the held schema writer'
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
                app.state.ctx.storage_maintenance.phase = 'schema'
                writer = executor.submit(hold_writer)
                response = None
                try:
                    assert entered.wait(2)
                    response = executor.submit(client.get, '/')
                    # The lock hook rejects every blocking serving-lock acquisition.
                    # Allow cold template compilation within the first-page deadline.
                    home = response.result(timeout=5)
                    assert home.status_code == 200 and 'A comfort mix for you' in home.text
                    assert_maintenance_banner(home)
                    assert 'id="growth-title"' in home.text
                    assert not writer.done()
                finally:
                    release.set()
                    writer.result(timeout=5)
                    if response is not None:
                        response.result(timeout=5)
                    app.state.ctx.storage_maintenance.phase = 'done'
            assert list(map(tuple, library.conn.execute('SELECT * FROM rec_impressions ORDER BY surface,item_key'))) == before
            assert library.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
            assert not library.activity_paused
            # The same running app resumes ordinary visits after admission is free.
            old_views = library.rec_ledger.card_views('comfort')
            assert client.get('/').status_code == 200
            assert library.rec_ledger.card_views('comfort') == old_views + 1
            assert library.rec_proposals.get_proposals('home_choice')['experiments']


@pytest.mark.parametrize('phase', ['file_read', 'context_expansion'])
@pytest.mark.parametrize('endpoint', ['home', 'playlists', 'status', 'settings', 'assembly', 'radio', 'retry'])
def test_inventory_preparation_keeps_each_foreground_consumer_responsive(
        inventory_serving_input, tmp_path, monkeypatch, phase, endpoint):
    with inventory_serving_input.open(tmp_path / 'inventory.db') as library:
        oid, surface = historical_inventory(library, 'inventory-home')
        path = library.rec_ledger.evidence()._path('opportunity', oid)
        original = path.read_bytes()
        entered, release = threading.Event(), threading.Event()
        context = group_roots.context

        def hold():
            if not entered.is_set():
                entered.set()
                assert release.wait(3), 'Foreground consumer waited for inventory preparation'

        def held_read(candidate, **kwargs):
            if candidate == path:
                hold()
            return read_bytes(candidate, **kwargs)

        def held_context(store, captured, **kwargs):
            if captured.get('artifact') and threading.current_thread() is not threading.main_thread():
                hold()
            return context(store, captured, **kwargs)

        patch_byte_reads(monkeypatch, held_read if phase == 'file_read' else read_bytes)
        monkeypatch.setattr(group_roots, 'context', held_context if phase == 'context_expansion' else context)
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
                        assert response.status_code == 200 and 'A comfort mix for you' in response.text
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
                        stored_home.unmodeled_cards(library, 1000, card_builder=home_cards._carded)
                        stored_home.assemble(library, 1000, 0)
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
                    assert library.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0] == 0
                finally:
                    release.set()
                result = future.result(5)
            patch_byte_reads(monkeypatch, read_bytes)
            monkeypatch.setattr(group_roots, 'context', context)
            for _ in range(10):
                if not result.get('deferred'):
                    break
                result = choice_evidence.maintain_storage(library, 1000)
            assert not result.get('deferred')
            assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='offer' AND external_key=?",
                                            (oid,)).fetchone()
            owner = 'proposal:' + surface + ':context'
            assert [row[0] for row in library.conn.execute(
                "SELECT external_version FROM group_evidence_versions WHERE kind='comparison' AND external_key=? ORDER BY external_version",
                (owner,))] == ['first-leaf', 'first-parent']
            assert library.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0] == 1
            assert path.read_bytes() == original and not library.activity_paused
            assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()
        finally:
            release.set()
            worker.shutdown()
            client.close()


def test_historical_recovery_resumes_new_choices_and_worker_measurement_flush(
        inventory_serving_input, tmp_path, monkeypatch):
    with inventory_serving_input.open(tmp_path / 'resumed-evidence.db') as library:
        historical_inventory(library, 'resumed-prepared-home')
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
