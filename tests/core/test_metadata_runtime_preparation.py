"""Preparation can restart, refuses stale cutover and leaves ordinary work alone."""
import json
import time

import pytest
from tests.metadata_provenance_support import metadata_provenance
from tests.store_fixture_support import (
    bounded_consumer_schema,
    file_consumer_store as file_backed_store,
    without_query_timing,
)

from yt_playlist.core import catalog, fao, metadata_legacy, metadata_native, metadata_runtime, metadata_runtime_files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.preparation import metadata_runtime as preparation
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Metadata conversion does not construct the HTTP application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    """Keep real metadata owners and guards with only the vocabulary used here."""
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as schema:
        yield schema


def seeded(store):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
    now = time.time()
    store.enrichment.apply_provider_fields(entry, 'a', {'genre':'Jazz','bpm':120}, providers=('a',))
    store.enrichment_state.record_enrichment_attempt('song|artist', 'a', {'genre':'Jazz','bpm':120}, video_id='video', now=now)
    with store.conn:
        catalog.choose_field(store.conn, entry, 'bpm', None, now=now)
        catalog.choose_field(store.conn, entry, 'bpm', 130, now=now+1)
        catalog.choose_field(store.conn, entry, 'genre', 'Jazz', now=now+2)
    store.enrichment.upsert_conflict(entry, 'genre', [{'provider':'a','value':'Jazz'},{'provider':'b','value':'Rock'}], now=now)
    store.enrichment_state.record_metadata_observations(entry, 'native', [MetadataObservation('mood_party', .8,
        'recording', 'native-id', 'prediction', unit='classifier_probability')], run_id='native', now=now)
    return store, entry


def run(store, path, *, batch_size=1):
    for _ in range(100):
        result = preparation.step(store.conn, path, batch_size=batch_size)
        if result['complete']:
            return result
    raise AssertionError('Preparation failed to finish')


def test_native_source_reads_shared_delivery_once_and_checks_each_observation(file_backed_store, monkeypatch):
    from yt_playlist.core import metadata_deliveries
    from yt_playlist.core.activity import WorkBudgetExceeded

    store = file_backed_store
    try:
        entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
        store.enrichment_state.record_metadata_observations(entry, 'native', [
            MetadataObservation(field, value, 'recording', 'native-id', 'prediction', unit='classifier_probability')
            for field, value in (('energy', .3), ('mood_party', .8))], run_id='native', now=100)
        key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        response, reads = metadata_deliveries.response, []
        damaged = False

        def counted(conn, delivery):
            reads.append(delivery)
            body = response(conn, delivery)
            if damaged:
                for observation in body['observations']:
                    if observation['field'] == 'mood_party':
                        observation['unit'] = 'wrong'
            return body

        monkeypatch.setattr(metadata_deliveries, 'response', counted)
        values = metadata_native.installed_source_values(store.conn, key)
        assert {name: item['value'] for name, item in values.items()} == {'energy': .3, 'mood_party': .8}
        assert len(reads) == 1
        reads.clear()
        with pytest.raises(WorkBudgetExceeded):
            metadata_native.installed_source_values(store.conn, key, max_bytes=1)
        assert not reads
        damaged = True
        with pytest.raises(ValueError, match='reference differs'):
            metadata_native.installed_source_values(store.conn, key)
        assert len(reads) == 1
    finally:
        store.conn.close()


def test_file_batch_overlaps_document_preparation_with_durable_writes(store, monkeypatch):
    from threading import Event

    started, preparing_next = Event(), Event()
    write = fao.write

    def writing(*args, **kwargs):
        started.set()
        assert preparing_next.wait(3), 'Document preparation did not overlap file writes'
        return write(*args, **kwargs)

    def documents():
        yield 'native', ('first',), 1, {'observations': {'energy': .3}}
        assert started.wait(3), 'File publication waited for the entire batch'
        preparing_next.set()
        yield 'native', ('second',), 1, {'observations': {'energy': .8}}

    monkeypatch.setattr(fao, 'write', writing)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        metadata_runtime_files.publish_many(store.conn, documents())
    for key, value in [('first', .3), ('second', .8)]:
        assert metadata_runtime_files.read(store.conn, 'native', (key,), 1) == {
            'observations': {'energy': value}}


@pytest.mark.parametrize('failure', ['producer', 'owner'])
def test_file_batch_joins_writers_when_document_preparation_fails(store, monkeypatch, failure):
    from threading import Event

    started, release, finished = Event(), Event(), Event()
    write = fao.write

    def writing(*args, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return write(*args, **kwargs)
        finally:
            finished.set()

    def documents():
        yield 'native', ('first',), 1, {'observations': {}}
        assert started.wait(3)
        release.set()
        if failure == 'producer':
            raise ValueError('Document preparation failed')
        yield 'native', (0,), 1, {'observations': {}}

    monkeypatch.setattr(fao, 'write', writing)
    with pytest.raises(ValueError, match='Document preparation failed|Invalid runtime metadata primary key'):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            metadata_runtime_files.publish_many(store.conn, documents())
    assert finished.is_set()
    assert not store.conn.in_transaction


def test_failed_batch_joins_file_writers_before_rolling_back_heads(file_backed_store, monkeypatch):
    from threading import Event, Lock

    store, entry = seeded(file_backed_store)
    try:
        for number in range(3):
            store.enrichment_state.record_metadata_observations(entry, 'native-' + str(number), [
                MetadataObservation('energy', .3, 'recording', str(number), 'prediction',
                                    unit='classifier_probability')], run_id='native', now=100)
        assert store.conn.execute('SELECT count(*) FROM metadata_native_delivery_heads').fetchone()[0] == 0
        original = fao.write
        first_started, second_failed, first_finished = Event(), Event(), Event()
        lock, calls = Lock(), []

        def fail_during_durable_writes(*args, **kwargs):
            with lock:
                position = len(calls)
                calls.append(args[0])
            if position == 0:
                first_started.set()
                assert second_failed.wait(3), 'File writes did not overlap'
                try:
                    return original(*args, **kwargs)
                finally:
                    first_finished.set()
            if position == 1:
                assert first_started.wait(3)
                second_failed.set()
                raise OSError('Durable file publication failed')
            return original(*args, **kwargs)

        monkeypatch.setattr(fao, 'write', fail_during_durable_writes)
        with pytest.raises(OSError, match='Durable file publication failed'):
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                preparation.prepare_batch(store.conn, 'native', None, 128, at=time.time())
        assert first_finished.is_set()
        assert store.conn.execute('SELECT count(*) FROM metadata_native_delivery_heads').fetchone()[0] == 0
        monkeypatch.setattr(fao, 'write', original)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            _, count = preparation.prepare_batch(store.conn, 'native', None, 128, at=time.time())
        assert count == 4
        for row in store.conn.execute('SELECT source_key,revision FROM rec_musical_sources'):
            assert metadata_runtime_files.read(store.conn, 'native', (row[0],), row[1])['observations']
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_file_batch_preserves_the_callers_cancellation_scope(store):
    from threading import get_ident
    from yt_playlist.core import activity

    caller = get_ident()
    path = metadata_runtime_files.directory(store.conn) / metadata_runtime_files.relative('native', ('source',), 1)
    with pytest.raises(activity.WorkClosing):
        with activity.cancellable_work(lambda: get_ident() != caller):
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                metadata_runtime_files.publish_many(store.conn, [('native', ('source',), 1, {'observations': {}})])
    assert not path.exists()


def test_preparation_preserves_real_reader_outputs_queues_and_reopens(file_backed_store, tmp_path):
    store, entry = seeded(file_backed_store)
    path = tmp_path / 'progress.json'
    try:
        # An imported decision has an unknown time and need not be the current
        # head. Preserve both facts rather than assigning conversion time.
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            metadata_legacy.copy_observations(store.conn, 'metadata_decisions', [{'_legacy_id': 1,
                'entry_id': entry, 'field': 'bpm', 'value_json': 'null', 'source': 'legacy', 'chosen_at': None}])
        catalog_before = [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]
        genre_before = store.enrichment_reports.genre_provenance(entry)
        key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        native_before = metadata_native.source_values(store.conn, key)
        queues = {name:list(map(tuple, store.conn.execute('SELECT * FROM '+name))) for name in
                  ('rec_change_queue','rec_content_pending','rec_musical_pending','enrichment_provider_jobs')}
        # This check exercises exact conversion and restart, rather than the
        # separately covered one-row checkpoint/replay boundary.
        result = run(store, path, batch_size=128)
        assert result['counts']['choices'] == 2
        assert not metadata_runtime.active(store.conn)
        preparation.activate(store.conn, path)
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')] == catalog_before
        assert metadata_native.source_values(store.conn, key) == native_before
        assert store.enrichment_reports.genre_provenance(entry) == genre_before
        decisions = metadata_provenance(store, entry)['decisions']
        assert len(decisions) == 4
        assert any(row['source'] == 'legacy' and row['chosen_at'] is None and row['value_json'] == 'null' for row in decisions)
        for name, before in queues.items():
            assert list(map(tuple, store.conn.execute('SELECT * FROM '+name))) == before
        store.conn.close()
        store = Store(file_backed_store._db_path)
        store.init_schema()
        assert metadata_runtime.active(store.conn)
        assert metadata_native.source_values(store.conn, key) == native_before
        assert store.enrichment_reports.genre_provenance(entry) == genre_before
        assert metadata_provenance(store, entry)['decisions'] == decisions
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_committed_batch_with_lost_progress_can_be_replayed(file_backed_store, tmp_path):
    store, _ = seeded(file_backed_store)
    path = tmp_path / 'progress.json'
    try:
        run(store, path)
        state = json.loads(fao.read(path).bytes)
        state.update(stage=2, cursor=None)
        fao.write(path, json.dumps(state).encode())
        assert json.loads(fao.read(path).bytes) == state
        assert state['stage'] == 2 and state['cursor'] is None
        counts = store.conn.execute('SELECT id,revision FROM metadata_runtime_choices ORDER BY id').fetchall()
        assert counts
        run(store, path)
        assert list(map(tuple, counts)) == list(map(tuple, store.conn.execute('SELECT id,revision FROM metadata_runtime_choices ORDER BY id')))
        preparation.activate(store.conn, path)
        assert metadata_runtime.active(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


@pytest.mark.parametrize('damage', ('native_value', 'native_provenance', 'native_missing_field', 'native_extra_field',
                                   'choice_null', 'choice_scope', 'choice_time', 'alternatives', 'conflict',
                                   'attribution', 'choice_head', 'extra_choice', 'freshness'))
def test_activation_rejects_semantic_changes_even_with_valid_checksums(file_backed_store, tmp_path, damage):
    store, entry = seeded(file_backed_store)
    path = tmp_path/'progress.json'
    try:
        run(store, path)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            if damage.startswith('native_'):
                source = store.conn.execute('SELECT source_key,revision FROM rec_musical_sources').fetchone()
                kind, owner, revision = 'native', (source[0],), source[1]
            elif damage.startswith('choice_') and damage != 'choice_head':
                choice = store.conn.execute('SELECT id FROM metadata_runtime_choices WHERE field=?',
                                            (metadata_runtime.code('bpm'),)).fetchone()
                kind, owner, revision = 'choices', (choice[0],), 1
            elif damage == 'alternatives':
                recording, provider, at = store.conn.execute('SELECT s.recording_id,p.id,s.at '
                    'FROM enrichment_provider_state s JOIN metadata_runtime_providers p ON p.name=s.provider').fetchone()
                kind, owner, revision = 'alternatives', (recording, provider), float(at)
            elif damage == 'conflict':
                kind, owner, revision = 'conflicts', (store.conn.execute('SELECT id FROM metadata_runtime_conflicts').fetchone()[0],), 1
            else:
                kind = None
            if kind:
                value = metadata_runtime_files.read(store.conn, kind, owner, revision)
                if damage == 'native_value':
                    value['observations']['mood_party']['value'] = .1
                elif damage == 'native_provenance':
                    value['observations']['mood_party']['provenance']['entity_id'] = 'other'
                elif damage == 'native_missing_field':
                    value['observations'].clear()
                elif damage == 'native_extra_field':
                    value['observations']['energy'] = value['observations']['mood_party']
                elif damage == 'choice_null':
                    value['value'] = 999
                elif damage == 'choice_scope':
                    value['input_video_id'] = 'other'
                elif damage == 'choice_time':
                    value['at'] += 100
                elif damage == 'alternatives':
                    value['fields']['bpm'] = 999
                else:
                    value['candidates'][0]['value'] = 'Rock'
                metadata_runtime_files.publish(store.conn, kind, owner, revision, value)
            elif damage == 'attribution':
                store.conn.execute('DELETE FROM metadata_runtime_recording_sources')
            elif damage == 'choice_head':
                store.conn.execute('UPDATE metadata_runtime_choices SET number_value=999 WHERE kind=4')
            elif damage == 'extra_choice':
                store.conn.execute('INSERT INTO metadata_runtime_choices(entry_id,field,provider_id,revision,kind) '
                    'SELECT entry_id,?,provider_id,1,1 FROM metadata_runtime_choices LIMIT 1', (metadata_runtime.code('album'),))
            else:
                store.conn.execute('UPDATE metadata_native_delivery_heads SET accepted_at=accepted_at+1')
        with pytest.raises(ValueError, match='differs from source'):
            preparation.activate(store.conn, path)
        assert not metadata_runtime.active(store.conn)
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 130
    finally:
        store.conn.close()


def test_cutover_verifies_without_writer_reservation_and_fences_concurrent_updates(file_backed_store, tmp_path, monkeypatch):
    from yt_playlist.core.preparation import metadata_runtime_verification
    from yt_playlist.core.db_handles import connect
    store, entry = seeded(file_backed_store)
    path = tmp_path/'progress.json'
    try:
        run(store, path)
        verify = metadata_runtime_verification.verify
        def concurrent(conn, state):
            verify(conn, state)
            # This must commit while verification still owns its read snapshot.
            other = connect(store._db_path)
            try:
                with other:
                    other.execute("INSERT INTO settings(key,value) VALUES ('concurrent-cutover-check','saved')")
            finally:
                other.close()
        monkeypatch.setattr(metadata_runtime_verification, 'verify', concurrent)
        with pytest.raises(ValueError, match='changed during verification'):
            preparation.activate(store.conn, path)
        assert not metadata_runtime.active(store.conn)
        assert store.settings.get_setting('concurrent-cutover-check') == 'saved'
    finally:
        store.conn.close()


def test_continuing_installed_writes_refuse_stale_activation(file_backed_store, tmp_path):
    store, entry = seeded(file_backed_store)
    path = tmp_path / 'progress.json'
    try:
        preparation.step(store.conn, path, batch_size=1)
        with store.conn:
            catalog.choose_field(store.conn, entry, 'bpm', 140)
        run(store, path)
        with pytest.raises(ValueError, match='source changed'):
            preparation.activate(store.conn, path)
        assert not metadata_runtime.active(store.conn)
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 140
    finally:
        store.conn.close()


def test_preparation_retains_newer_identical_native_delivery_freshness(file_backed_store, tmp_path):
    store = file_backed_store
    try:
        entry = store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)
        observation = MetadataObservation('mood_party', .8, 'recording', 'native-id', 'prediction',
                                          unit='classifier_probability')
        for name, at in (('first', 10), ('newest', 30)):
            store.enrichment_state.record_metadata_observations(entry, 'native', [observation], run_id=name, now=at)
        progress = tmp_path/'progress.json'
        run(store, progress)
        preparation.activate(store.conn, progress)
        late = MetadataObservation('mood_party', .1, 'recording', 'native-id', 'prediction',
                                   unit='classifier_probability')
        store.enrichment_state.record_metadata_observations(entry, 'native', [late], run_id='late', now=20)
        key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        assert metadata_native.source_values(store.conn, key)['mood_party']['value'] == .8
        with pytest.raises(ValueError, match='cannot change its result'):
            store.enrichment_state.record_metadata_observations(entry, 'native', [late], run_id='newest', now=30)
    finally:
        store.conn.close()
