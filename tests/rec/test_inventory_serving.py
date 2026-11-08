"""Each foreground consumer stays usable during real installed inventory work."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.file_content_support import patch_byte_reads, read_bytes
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import returning_listener, without_metadata_providers
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.rec.test_evidence_responsiveness import (
    assert_maintenance_banner, historical_inventory, publish_serving_inputs,
    seed_evidence_library, serving_bytecode, serving_routes)
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.migrations import regular, runner
from yt_playlist.rec import choice_evidence, home_cache, home_prepared, group_roots
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


def test_inventory_cached_home_serves_every_lane_before_recovery(inventory_serving_input, tmp_path, monkeypatch):
    # Own the ordinary cached-page contract and its first real template compile;
    # held preparation below then isolates each concurrent foreground request.
    with inventory_serving_input.open(tmp_path / 'cached-home.db') as library:
        app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
        worker = app.state.ctx.rec_worker
        monkeypatch.setattr(worker, '_start_loop', lambda: None)
        client = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
        try:
            response = client.get('/')
            assert response.status_code == 200
            for lane in ('comfort', 'wheelhouse', 'explore', 'temporal'):
                assert f'A {lane} mix for you' in response.text
            assert_maintenance_banner(response)
            assert not library.activity_paused
        finally:
            worker.shutdown()
            client.close()


@pytest.mark.parametrize('phase', ['file_read', 'context_expansion'])
@pytest.mark.parametrize('endpoint', ['home', 'playlists', 'status'])
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
                    else:
                        response = client.get('/storage-maintenance/status')
                        assert response.status_code == 200
                        status = response.json()
                        assert not status['active'] and status['evidence_maintenance']['running']
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
