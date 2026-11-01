"""Recovery owns publication, while foreground reads use the last committed state."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_catalog_track
from tests.core.test_group_content import prepared
from tests.onboarding_support import publish_personal_mix
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.evidence_ledger import canonical, EvidenceBudgetExceeded, EvidenceLedger
from yt_playlist.core.storage_maintenance import EVIDENCE_MESSAGE, EVIDENCE_RETRY_MESSAGE
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence, choice_retention, group_root_backfill, group_roots, home_cards, mode_surfaces, stored_home
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.rec.choice_context import checkpoint, CORRECTION_CONTRACT
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.rec.worker_queue import Job
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos import group_queries
from yt_playlist.web.app import create_app


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path / 'library.db')
    store.init_schema()
    for key in ('one', 'two'):
        seed_catalog_track(store, key, key, 'Artist', video_id=key, genre='house')
    value = prepared('first', 1)
    value.update(unknown=[], edges=[], lineage=[])
    for group in value['groups']:
        group['payload'] = json.dumps({**json.loads(group['payload']), 'size': len(group['members'])})
    owner = store.taste_groups.stage(value)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.taste_groups.finish_staged('first', owner)
    choice_evidence.issue(store, 'mixes', [{'id': 'one'}, {'id': 'two'}], 10,
        context={'at': 10, 'group_generation': 'first', 'musical_groups': [
            {'group_id': 'stable-leaf', 'version': 'first-leaf', 'parent_version': 'first-parent'}]})
    try:
        yield store
    finally:
        store.conn.close()


def test_correction_identity_matches_full_definition_without_decoding_files(library, monkeypatch):
    head = library.taste_groups.head()
    expected_head = {'generation': head['generation'], 'groups': [
        {key: group.get(key, False) for key in ('group_id', 'version', 'parent_version', 'legacy')}
        for group in head['groups']]}
    for version in ('first-parent', 'first-leaf'):
        original = library.taste_groups.definition(version, generation='first')
        expected = {key: original[key] for key in (
            'group_id', 'version', 'generation', 'created_at', 'parent_version')}
        assert library.taste_groups.correction_definition(version, generation='first') == expected
    assert library.taste_groups.correction_definition('first-leaf', generation='other') is None
    monkeypatch.setattr(group_queries, '_definition', lambda *a: pytest.fail('Decoded a diagnostic graph'))
    monkeypatch.setattr(group_queries, '_generation', lambda *a: pytest.fail('Decoded model inputs'))
    assert library.taste_groups.learning_head() == expected_head
    acknowledged = choice_retention.acknowledge(library, 11, lambda *a: {'watermark': 'published'})
    assert len(acknowledged) == 1


def test_learning_recovers_only_relevant_legacy_ancestry_on_one_pinned_reader(library, monkeypatch):
    definitions = [library.taste_groups.correction_definition(version, generation='first')
                   for version in ('first-parent', 'first-leaf')]
    oid = choice_evidence.issue(library, 'mixes', [
        {'id': 'leaf', 'group_version': 'first-leaf'}], 12,
        context={'at': 12, 'group_generation': 'first', 'musical_groups': [
            {'group_id': row['group_id'], 'version': row['version']} for row in definitions]})
    assert choice_evidence.respond(library, oid, 'visible', 13, visible=['leaf'])
    assert choice_evidence.respond(library, oid, 'choice', 14, option_id='leaf')
    expected = choice_evidence.compile_report(library, 15)
    ledger = library.rec_ledger.evidence()
    identity = ledger.get('opportunity', oid)['context']
    context = {'at': 12, 'group_generation': 'first', 'musical_groups': [
        {'group_id': row['group_id'], 'version': row['version']} for row in definitions],
        'musical_definitions': [{key: value for key, value in row.items() if key != 'parent_version'}
                               for row in definitions]}
    # Controlled early-v1 checkpoint: unrelated history is unknown and must
    # not be loaded merely to learn from the one actually served alternative.
    context['musical_definitions'].extend({'group_id': f'other-{i}', 'version': f'other-{i}',
        'generation': 'first', 'created_at': 1} for i in range(500))
    ledger.put('checkpoint', identity, {'schema': 1, 'contract': CORRECTION_CONTRACT,
        'source_context': identity, 'context': context, 'digest': hashlib.sha256(canonical(context)).hexdigest(),
        'watermark': expected['watermark'], 'incorporated_at': 15})
    lookup, readers = CompactTasteGroupsRepo.correction_definition, set()

    def relevant(self, version, **kwargs):
        assert version in ('first-parent', 'first-leaf')
        assert self._store._read_only
        readers.add(id(self._store))
        return lookup(self, version, **kwargs)

    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', relevant)
    monkeypatch.setattr(group_queries, '_definition', lambda *a: pytest.fail('Decoded a diagnostic graph'))
    monkeypatch.setattr(group_queries, '_generation', lambda *a: pytest.fail('Decoded model inputs'))
    actual = choice_evidence.compile_report(library, 15)
    assert actual == expected
    assert len(readers) == 1


def test_small_inventory_batches_advance_before_repeating_the_whole_ledger_scan(library, monkeypatch):
    monkeypatch.setattr(group_root_backfill, 'BATCH_ROWS', 1)
    monkeypatch.setattr(group_root_backfill, 'BATCH_BYTES', 1)
    publish, calls = choice_evidence.compile_report, []

    def counted(*args, **kwargs):
        assert library.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0]
        calls.append(True)
        return publish(*args, **kwargs)

    monkeypatch.setattr(choice_evidence, 'compile_report', counted)
    result = choice_evidence.maintain_storage(library, 11)
    assert not result.get('deferred')
    assert calls == [True]
    assert not library.activity_paused
    offer, = library.rec_ledger.evidence().records('opportunity')
    assert checkpoint(library.rec_ledger.evidence(), offer['context'])


@pytest.mark.parametrize('failure', [WorkBudgetExceeded, WorkClosing, OSError])
def test_pending_recovery_survives_interruption_and_clears_only_after_success(library, monkeypatch, failure):
    path = library._db_path
    original = choice_evidence._maintain_storage
    monkeypatch.setattr(choice_evidence, '_maintain_storage', lambda *a, **kw: (_ for _ in ()).throw(failure('stop')))
    if failure is WorkBudgetExceeded:
        assert choice_evidence.maintain_storage(library, 11) == {'deferred': True}
    else:
        with pytest.raises(failure):
            choice_evidence.maintain_storage(library, 11)
    assert library.activity_paused and library._evidence_maintenance == 0
    assert library.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
    app = create_app(library, lambda: {}, background_work=False)
    status = app.state.ctx.storage_maintenance.status()
    assert status['active'] and not status['recording']
    assert status['message'] == (EVIDENCE_MESSAGE if failure is WorkBudgetExceeded else EVIDENCE_RETRY_MESSAGE)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', original)
    # A new Store and worker reconstruct intent from committed state, without
    # inheriting an iterator, cache, old thread or old retry timestamp.
    reopened = Store(path)
    reopened.init_schema()
    restarted = create_app(reopened, lambda: {}, background_work=True)
    worker = restarted.state.ctx.rec_worker
    assert reopened.activity_paused
    scheduled = []
    monkeypatch.setattr(worker, '_start_loop', lambda: scheduled.append(True))
    worker._poll_evidence_storage()
    assert scheduled and worker.jobs.has('evidence_storage')
    for _ in range(10):
        result = choice_evidence.maintain_storage(reopened, 11)
        if not result.get('deferred'):
            break
    else:
        pytest.fail('Committed evidence recovery did not finish after restart')
    assert reopened.settings.get_setting(choice_evidence.STORAGE_PENDING) == '0'
    assert not reopened.activity_paused
    assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    assert reopened.taste_groups.definition('first-leaf', generation='first')
    reopened.conn.close()


def test_home_and_navigation_remain_available_while_processing_historical_definitions(library, monkeypatch):
    library.accounts.upsert_account('Main', 'bridge', None, True)
    library.settings.set_setting('last_sync_at', '1000')
    library.settings.set_setting('onboard_dismissed', '1')
    publish_personal_mix(library)
    mode_surfaces.prepare_bundles(library, 1000)
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
    worker = app.state.ctx.rec_worker
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    entered, release = threading.Event(), threading.Event()
    definition = CompactTasteGroupsRepo.correction_definition

    def held(self, *args, **kwargs):
        value = definition(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', held)
    # The real worker and capture queue are enabled; only the ticker is not
    # started. This executor runs the actual storage path under its admission.
    client = TestClient(app, base_url='http://127.0.0.1')
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(choice_evidence.maintain_storage, library, 1000)
        try:
            assert entered.wait(2)
            started = time.monotonic()
            home = client.get('/')
            assert home.status_code == 200 and 'id="storage-maintenance"' not in home.text
            for lane in ('comfort', 'wheelhouse', 'explore', 'temporal'):
                assert f'A {lane} mix for you' in home.text
            assert choice_evidence.maintain_storage(library, 1000) == {'deferred': True}
            assert client.get('/playlists').status_code == 200
            assert client.post('/sync').status_code == 503
            # This path used to acquire a durable pending root before opening
            # its private reader, and therefore waited for all storage work.
            stored_home.unmodeled_cards(library, 1000, card_builder=home_cards._carded)
            stored_home.assemble(library, 1000, 0)
            with pytest.raises(WorkBudgetExceeded):
                PreparedRadio(library, 1000)
            assert time.monotonic() - started < 2
            assert not future.done()
        finally:
            release.set()
        future.result(5)
    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', definition)
    for _ in range(10):
        if not choice_evidence.maintain_storage(library, 1000).get('deferred'):
            break
    assert not client.get('/storage-maintenance/status').json()['active']
    oid = choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 1001, context={})
    assert oid and choice_evidence.respond(library, oid, 'choice', 1002, option_id='one')
    worker._flush_measurements()
    assert library.rec_ledger.evidence().get('response', oid + ':choice:one')
    assert library.taste_groups.definition('first-leaf', generation='first')
    worker.shutdown()
    client.close()


@pytest.mark.parametrize('phase', ['file_read', 'context_expansion'])
def test_inventory_preparation_does_not_hold_the_home_serving_lock(library, monkeypatch, phase):
    library.accounts.upsert_account('Main', 'bridge', None, True)
    library.settings.set_setting('last_sync_at', '1000')
    library.settings.set_setting('onboard_dismissed', '1')
    publish_personal_mix(library)
    mode_surfaces.prepare_bundles(library, 1000)
    # Controlled predecessor capture: real immutable evidence lacking only its
    # new SQL owner. The collector fence remains closed throughout preparation.
    oid = choice_evidence._issue_record(library, {'id': 'inventory-home', 'format': 'mixes',
        'at': 10, 'observed_at': 10, 'options': [{'id': 'one'}]},
        {'at': 10, 'group_generation': 'first', 'musical_groups': [
            {'group_id': 'stable-leaf', 'version': 'first-leaf'}]})
    path = library.rec_ledger.evidence()._path('opportunity', oid)
    entered, release = threading.Event(), threading.Event()

    def hold():
        if not entered.is_set():
            entered.set()
            assert release.wait(5)

    read_bytes, context = Path.read_bytes, group_roots.context

    def held_read(self):
        if self == path:
            hold()
        return read_bytes(self)

    def held_context(store, captured, **kwargs):
        if captured.get('artifact') and threading.current_thread() is not threading.main_thread():
            hold()
        return context(store, captured, **kwargs)

    monkeypatch.setattr(Path, 'read_bytes', held_read if phase == 'file_read' else read_bytes)
    monkeypatch.setattr(group_roots, 'context', held_context if phase == 'context_expansion' else context)
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
    worker = app.state.ctx.rec_worker
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    client = TestClient(app, base_url='http://127.0.0.1')
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(choice_evidence.maintain_storage, library, 1000)
        try:
            assert entered.wait(2)
            started = time.monotonic()
            response = client.get('/')
            assert response.status_code == 200 and 'id="storage-maintenance"' not in response.text
            assert 'A mix for you' in response.text
            assert client.get('/playlists').status_code == 200
            assert client.get('/storage-maintenance/status').json()['active']
            assert time.monotonic() - started < 2
            assert not future.done()
            assert library.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0] == 0
        finally:
            release.set()
        future.result(5)
    for _ in range(10):
        if not choice_evidence.maintain_storage(library, 1000).get('deferred'):
            break
    assert library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='offer' AND external_key=?",
                                (oid,)).fetchone()
    assert not library.activity_paused
    assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()
    worker.shutdown()
    client.close()


def test_home_onboarding_and_growth_do_not_wait_for_a_schema_writer(library, monkeypatch):
    from yt_playlist.util import genre_map

    library.accounts.upsert_account('Main', 'bridge', None, True)
    library.settings.set_setting('last_sync_at', '1000')
    publish_personal_mix(library)
    mode_surfaces.prepare_bundles(library, 1000)
    prepared_home = library.rec_proposals.get_proposals('home_tuned')
    families = genre_map.all_families()
    prepared_home['fingerprint'] = {'breadth': {'families': {
        family: 1 / len(families) for family in families}, 'breadth': 1},
        'eras': [], 'transient': {}}
    library.rec_proposals.put_proposals('home_tuned', prepared_home, 1000)
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=False)
    entered, release = threading.Event(), threading.Event()
    library._schema_recovery_pending = True
    acquire = library._lock.acquire

    def foreground_acquire(blocking=True, timeout=-1):
        if entered.is_set() and not release.is_set() and not library._lock.held_by_current_thread:
            assert not blocking, 'Home tried to wait for the held schema writer'
        return acquire(blocking=blocking, timeout=timeout)

    monkeypatch.setattr(library._lock, 'acquire', foreground_acquire)

    def hold_writer():
        with library._lock:
            with library.conn:
                library.conn.execute('BEGIN IMMEDIATE')
                entered.set()
                assert release.wait(10)

    with TestClient(app, base_url='http://127.0.0.1') as client, ThreadPoolExecutor(2) as executor:
        writer = executor.submit(hold_writer)
        response = None
        try:
            assert entered.wait(2)
            response = executor.submit(client.get, '/')
            # The lock hook rejects every blocking serving-lock acquisition.
            # Allow cold template compilation within the first-page deadline.
            home = response.result(timeout=5)
            assert home.status_code == 200 and 'A comfort mix for you' in home.text
            assert 'id="storage-maintenance"' not in home.text
            assert 'id="growth-title"' in home.text
            assert not writer.done()
        finally:
            release.set()
            writer.result(timeout=5)
            if response is not None:
                response.result(timeout=5)
            library._schema_recovery_pending = False


def test_playlists_keep_committed_account_metadata_while_schema_writer_is_held(library):
    main = library.accounts.upsert_account('Main', 'bridge-main', None, True)
    other = library.accounts.upsert_account('Other', 'bridge-other', None, False)
    audio = library.tracks.upsert_track('playlist-audio', 'Audio', 'Artist', None, 100,
        video_type='MUSIC_VIDEO_TYPE_ATV', thumbnail='/audio.png')
    video = library.tracks.upsert_track('playlist-video', 'Video', 'Artist', None, 100,
        video_type='MUSIC_VIDEO_TYPE_OMV', thumbnail='/video.png')
    first = library.playlists.upsert_playlist(main, 'LM', 'Main liked', 1, '', 1,
                                             thumbnail='/main-cover.png')
    second = library.playlists.upsert_playlist(other, 'LM', 'Other liked', 1, '', 1)
    generated = library.playlists.upsert_playlist(main, 'GENERATED', 'Generated mix', 1, '', 1)
    library.playlists.set_playlist_tracks(first, [audio])
    library.playlists.set_playlist_tracks(second, [video])
    library.playlists.set_playlist_tracks(generated, [video])
    library.playlists.set_playlist_group('LM', 'Main group', account_id=main)
    library.playlists.set_playlist_group('LM', 'Other group', account_id=other)
    library.playlists.mark_generated_playlist('GENERATED')
    library.playlists.set_playlist_group('GENERATED', 'Generated')
    library.playlist_recipes.set_recipe('GENERATED', {'media': 'video'}, now=900)
    library.playlists.upsert_playlist(main, 'HIDDEN', 'Hidden playlist', 0, '', 1)
    library.playlists.hide_playlist('HIDDEN')
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=False)
    entered, release = threading.Event(), threading.Event()
    paused = getattr(library, '_schema_recovery_pending', False)

    def hold_writer():
        with library._lock:
            library.conn.execute('BEGIN IMMEDIATE')
            try:
                library.conn.execute("UPDATE playlists SET title='Uncommitted',thumbnail='/uncommitted.png' "
                                     'WHERE id=?', (first,))
                library.conn.execute("UPDATE accounts SET label='Uncommitted account' WHERE id=?", (main,))
                library.conn.execute("UPDATE playlist_scopes SET group_name='Uncommitted group' "
                                     'WHERE ref=?', (f'@account:{main}:LM',))
                entered.set()
                assert release.wait(10), 'Playlists GET waited for the schema writer'
            finally:
                library.conn.rollback()

    with TestClient(app, base_url='http://127.0.0.1') as client:
        # Warm templates through the unchanged normal route before holding SQL.
        assert client.get('/playlists').status_code == 200
        library._schema_recovery_pending = True
        try:
            with ThreadPoolExecutor(2) as executor:
                writer = executor.submit(hold_writer)
                try:
                    assert entered.wait(2)
                    response = executor.submit(client.get, '/playlists').result(timeout=2)
                    assert response.status_code == 200 and 'playlistsTab(' in response.text
                    assert not writer.done() and not release.is_set()
                    rows = {row['id']: row for row in response.context['rows']}
                    assert set(rows) == {first, second, generated}
                    fields = ('title', 'ytm', 'identity', 'thumbnail', 'kind', 'group', 'is_generated', 'is_video')
                    assert tuple(rows[first][field] for field in fields) == (
                        'Main liked', 'LM', 'Main', '/main-cover.png', 'audio', 'Main group', False, False)
                    assert tuple(rows[second][field] for field in fields) == (
                        'Other liked', 'LM', 'Other', '/video.png', 'video', 'Other group', False, False)
                    assert tuple(rows[generated][field] for field in fields) == (
                        'Generated mix', 'GENERATED', 'Main', '/video.png', 'video', 'Generated', True, True)
                    assert rows[generated]['created'] is None
                    assert rows[generated]['generated_created'] == 900
                finally:
                    release.set()
                writer.result(timeout=2)
        finally:
            library._schema_recovery_pending = paused
    assert library.playlists.get_playlist(first).title == 'Main liked'


def test_cached_home_does_not_wait_for_inventory_file_preparation(library, monkeypatch, tmp_path):
    from tests.rec.test_group_root_backfill import historical_offer

    library.accounts.upsert_account('Main', 'bridge', None, True)
    library.settings.set_setting('last_sync_at', '1000')
    library.settings.set_setting('onboard_dismissed', '1')
    publish_personal_mix(library)
    mode_surfaces.prepare_bundles(library, 1000)
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=True)
    worker = app.state.ctx.rec_worker
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    client = TestClient(app, base_url='http://127.0.0.1')
    entered, release = threading.Event(), threading.Event()
    maintenance_thread = [None]
    observed = {'home_timed_out': False, 'cleanup_errors': []}
    try:
        # Establish real cached output before creating the controlled predecessor
        # inventory, so earlier setup and warming cannot consume the gated file.
        warm = client.get('/')
        assert warm.status_code == 200 and 'A mix for you' in warm.text
        identity = historical_offer(library, 'inventory-serving-check', captured={
            'at': 10, 'group_generation': 'first', 'musical_groups': [
                {'group_id': 'stable-parent', 'version': 'first-parent'},
                {'group_id': 'stable-leaf', 'version': 'first-leaf', 'parent_version': 'first-parent'}]})
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
        read_bytes = Path.read_bytes

        def held_read(candidate):
            if candidate == path and threading.get_ident() == maintenance_thread[0] and not entered.is_set():
                # The exact cursor proves this is inventory preparation, rather
                # than a later acknowledgement or an unrelated Home file read.
                assert backfill.pending_path == path
                entered.set()
                assert release.wait(10), 'Inventory test did not release its controlled file read'
            return read_bytes(candidate)

        def maintain():
            maintenance_thread[0] = threading.get_ident()
            return choice_evidence.maintain_storage(library, 1000)

        monkeypatch.setattr(Path, 'read_bytes', held_read)
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
                    assert home.status_code == 200 and 'A mix for you' in home.text
                    assert 'id="storage-maintenance"' not in home.text
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
        monkeypatch.setattr(Path, 'read_bytes', read_bytes)
        for _ in range(10):
            if not choice_evidence.maintain_storage(library, 1000).get('deferred'):
                break
        observed['roots_complete'] = library.conn.execute(
            'SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()[0]
        assert observed['roots_complete'] == 1
        assert backfill.stage == len(group_root_backfill.KINDS) and backfill.pending_path is None
        assert path.read_bytes() == raw
        assert owners <= {tuple(row) for row in library.conn.execute('SELECT * FROM group_evidence_owners')}
        assert [row[0] for row in library.conn.execute("SELECT external_version FROM group_evidence_versions "
            "WHERE kind='offer' AND external_key=? ORDER BY external_version", (identity,))] == [
                'first-leaf', 'first-parent']
        assert library.conn.execute("SELECT 1 FROM group_evidence_generations e JOIN group_generations g "
            "ON g.id=e.generation_id WHERE e.kind='offer' AND e.external_key=? AND g.external_key='first'",
            (identity,)).fetchone()
        assert not library.conn.execute('PRAGMA foreign_key_check').fetchone()
        observed['cursor_and_owner_checks_passed'] = True
        assert not observed['home_timed_out'], (
            'Cached Home waited more than 2 seconds for held inventory file preparation; '
            'see inventory-serving.json')
        assert not observed['cleanup_errors'], observed
    finally:
        release.set()
        (tmp_path / 'inventory-serving.json').write_text(json.dumps(observed, indent=2))
        worker.shutdown()
        client.close()


def test_admission_race_refuses_capture_and_cleans_its_lifetime_file(library, monkeypatch):
    jobs = []
    library.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    entered, release = threading.Event(), threading.Event()
    pending = group_roots.pending

    def late(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return pending(*args, **kwargs)

    monkeypatch.setattr(group_roots, 'pending', late)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(choice_evidence.issue, library, 'mixes', [{'id': 'one'}], 12,
                                 context={'group_generation': 'first'})
        try:
            assert entered.wait(2)
            with group_roots.admission(library):
                release.set()
                assert future.result(1) is None
                assert not jobs
        finally:
            release.set()
    directory = group_roots.group_documents.directory(library.conn) / '.pending'
    assert not list(directory.glob('*.lock'))
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()


def test_reader_close_during_maintenance_leaves_a_conservative_root_for_the_reaper(library):
    close = group_roots.pending(library, {'group_generation': 'first'}, 12)
    with ThreadPoolExecutor(1) as executor, group_roots.admission(library):
        executor.submit(close).result(1)
        assert library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    group_roots.reap_pending(library)
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    directory = group_roots.group_documents.directory(library.conn) / '.pending'
    assert not list(directory.glob('*.lock'))


def test_repeated_cutoffs_commit_checkpoints_and_resume_beyond_the_prefix_after_reopen(library, monkeypatch):
    for index in range(10):
        choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 20 + index,
            context={'at': 20 + index, 'group_generation': 'first', 'musical_groups': [
                {'group_id': 'stable-parent', 'version': 'first-parent'},
                {'group_id': 'stable-leaf', 'version': 'first-leaf'}]})
    ledger = library.rec_ledger.evidence()
    original = ledger.records('opportunity')
    definition = CompactTasteGroupsRepo.correction_definition
    reads = [0]

    def cutoff(self, *args, **kwargs):
        reads[0] += 1
        if reads[0] % 3 == 0:
            raise WorkBudgetExceeded('Controlled deadline during historical definition processing')
        return definition(self, *args, **kwargs)

    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', cutoff)
    reopened = None
    store, previous = library, 0
    try:
        for turn in range(30):
            try:
                choice_retention.acknowledge(store, 100, lambda *a: {'watermark': 'published'})
            except WorkBudgetExceeded:
                pass
            count = len(ledger.records('checkpoint'))
            assert count > previous or count == len(original)
            previous = count
            if turn == 2:
                assert store.settings.get_setting(choice_retention.AFTER)
                reopened = Store(library._db_path)
                reopened.init_schema()
                store = reopened
            if count == len(original):
                break
        assert previous == len(original)
        for offer in original:
            assert checkpoint(ledger, offer['context'])
            assert ledger.get('artifact', offer['context'])
    finally:
        if reopened is not None:
            reopened.conn.close()


def test_accepted_fifo_tail_keeps_roots_and_retries_while_recovery_is_pending(library, monkeypatch):
    app = create_app(library, lambda: {}, now_fn=lambda: 100, background_work=True)
    worker = app.state.ctx.rec_worker
    scheduled = []
    monkeypatch.setattr(worker, '_start_loop', lambda: scheduled.append(True))
    oid = choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 20,
                                context={'group_generation': 'first'})
    assert oid and choice_evidence.respond(library, oid, 'choice', 21, option_id='one')
    captures = list(worker.jobs.measurements)
    put, maintain = EvidenceLedger.put, choice_evidence._maintain_storage

    def full(self, kind, *args, **kwargs):
        if kind == 'opportunity':
            raise EvidenceBudgetExceeded('Controlled capacity failure', required_bytes=100)
        return put(self, kind, *args, **kwargs)

    def deferred(*args, **kwargs):
        raise WorkBudgetExceeded('Controlled recovery deadline')

    monkeypatch.setattr(EvidenceLedger, 'put', full)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', deferred)
    worker._flush_measurements()
    assert library.activity_paused
    assert list(worker.jobs.measurements) == captures
    assert library.choice_capture.get(oid)
    assert library.conn.execute("SELECT count(*) FROM group_evidence_owners WHERE kind='pending'").fetchone()[0] == 2
    monkeypatch.setattr(EvidenceLedger, 'put', put)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', maintain)
    worker.jobs.retry[Job.MEASUREMENT].retry_at = 0
    worker._running = False
    scheduled.clear()
    worker._retry_measurements()
    assert scheduled == [True]
    worker._run_pending()
    assert not worker.jobs.measurements and not library.choice_capture.opportunities()
    assert library.rec_ledger.evidence().get('response', oid + ':choice:one')
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    for _ in range(10):
        if not choice_evidence.maintain_storage(library, 100).get('deferred'):
            break
    assert not library.activity_paused
    worker.shutdown()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX application interruption signals')
@pytest.mark.parametrize('termination', [signal.SIGINT, signal.SIGKILL])
def test_process_interruption_releases_publication_and_restart_finishes_recovery(library, tmp_path, termination):
    gate = tmp_path / 'historical-definition-entered'
    code = textwrap.dedent('''
        import sys, threading
        from pathlib import Path
        from yt_playlist.core.store import Store
        from yt_playlist.rec import choice_evidence
        from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
        store = Store(sys.argv[1])
        store.init_schema()
        original = CompactTasteGroupsRepo.correction_definition
        def held(self, *args, **kwargs):
            value = original(self, *args, **kwargs)
            Path(sys.argv[2]).write_text('Historical definition read under publication admission')
            threading.Event().wait(30)
            return value
        CompactTasteGroupsRepo.correction_definition = held
        try:
            choice_evidence.maintain_storage(store, 100)
        except KeyboardInterrupt:
            sys.exit(130)
        finally:
            store.conn.close()
    ''')
    process = subprocess.Popen([sys.executable, '-c', code, str(library._db_path), str(gate)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not gate.exists():
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, 'Child did not enter historical definition processing'
            time.sleep(.02)
        process.send_signal(termination)
        _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == (130 if termination == signal.SIGINT else -signal.SIGKILL), stderr
        reopened = Store(library._db_path)
        try:
            reopened.init_schema()
            app = create_app(reopened, lambda: {}, background_work=True)
            assert reopened.activity_paused
            assert app.state.ctx.storage_maintenance.status()['active']
            for _ in range(10):
                if not choice_evidence.maintain_storage(reopened, 100).get('deferred'):
                    break
            assert not reopened.activity_paused
            assert not app.state.ctx.storage_maintenance.status()['active']
            assert reopened.taste_groups.definition('first-leaf', generation='first')
            assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
            app.state.ctx.rec_worker.shutdown()
        finally:
            reopened.conn.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
