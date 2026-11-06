"""Runtime metadata keeps bounded facts and reads optional detail from JSON."""
import hashlib
import json
import time

import pytest
from tests.metadata_provenance_support import metadata_provenance

from yt_playlist.core import catalog, fao, metadata, metadata_native
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core import metadata_runtime as runtime, metadata_runtime_files as files
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.core.store import Store


@pytest.fixture
def runtime_store(file_backed_store):
    store = file_backed_store
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        runtime.install(store.conn)
        runtime.activate(store.conn)
    return store


def entry(store, video='video'):
    identity = store.tracks.upsert_track(video, 'Song', 'Artist', None, 200)
    recording = runtime.subject(store.conn, identity)[0]
    return identity, recording


def test_repeated_provider_results_keep_current_facts_without_delivery_history(runtime_store):
    store = runtime_store
    track, recording = entry(store)
    at = time.time()
    for number in range(10):
        fields = {'genre': 'Jazz', 'bpm': 120+number, 'isrc': ['ONE']}
        captured, _ = store.enrichment_state.capture_provider_result(track, 'a', fields, run_id=str(number), now=at+number)
        for field, value in fields.items():
            store.enrichment_state.log_enrichment(track, str(number), 'a', field, value, delivery_id=captured)
        store.enrichment.apply_provider_fields(track, 'a', fields, providers=('a',), delivery_id=captured)
        store.enrichment_state.record_enrichment_attempt('song|artist', 'a', fields, video_id='video',
            run_id=str(number), delivery_id=captured, now=at+number)
    assert store.conn.execute('SELECT bpm,genre FROM catalog_tracks WHERE id=?', (track,)).fetchone()[:] == (129, 'Jazz')
    assert store.conn.execute('SELECT count(*) FROM enrichment_provider_state').fetchone()[0] == 1
    assert store.conn.execute('SELECT count(*) FROM metadata_runtime_recording_sources').fetchone()[0] == 2
    assert store.conn.execute('SELECT count(*) FROM recording_identifiers').fetchone()[0] == 1
    for table in ('metadata_deliveries', 'metadata_observations', 'provider_attempts', 'provider_state_fields'):
        assert store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
    assert store.enrichment_state.enrichment_provider_state(recording)['a']['fields']['bpm'] == 129
    assert store.enrichment_reports.genre_provenance(track)['options'] == [{'value': 'Jazz', 'providers': ['a']}]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_manual_null_blank_reset_and_rollback_orphan(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    store.enrichment.apply_provider_fields(track, 'a', {'bpm': 120}, providers=('a',))
    with pytest.raises(RuntimeError):
        with store.conn:
            catalog.choose_field(store.conn, track, 'bpm', 999)
            raise RuntimeError('interrupted')
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', None)
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] is None
    store.enrichment.apply_provider_fields(track, 'a', {'bpm': 135}, providers=('a',))
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] is None
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', '')
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] == ''
    with store.conn:
        catalog.withdraw_choice(store.conn, track, 'bpm')
    assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] == 135
    decisions = metadata_provenance(store, track)['decisions']
    assert [json.loads(row['value_json']) for row in decisions] == [None, '', None]
    assert decisions[-1]['withdrawn']


def test_expiry_preserves_completion_selected_values_and_model_inputs(runtime_store):
    store = runtime_store
    track, recording = entry(store)
    now = time.time()
    old = now-runtime.ALTERNATIVE_SECONDS-1
    store.enrichment.apply_provider_fields(track, 'a', {'genre': 'Jazz'}, providers=('a',))
    store.enrichment_state.record_enrichment_attempt('song|artist', 'a', {'genre': 'Jazz'}, now=old, video_id='video')
    observation = MetadataObservation('mood_party', .8, 'recording', 'native-id', 'prediction', unit='classifier_probability')
    store.enrichment_state.record_metadata_observations(track, 'native', [observation], run_id='native', now=old)
    key = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
    before = metadata_native.source_values(store.conn, key)
    with store.conn:
        assert runtime.expire(store.conn, now=now) == 1
    assert runtime.alternatives(store.conn, recording, 'a', now=now) == {}
    assert store.enrichment_state.enrichment_provider_state(recording)['a']['status'] == 'found'
    assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] == 'Jazz'
    assert metadata_native.source_values(store.conn, key) == before
    dependencies, references = store.musical_profiles.source_dependencies(key)
    assert references[0] == 'runtime'
    assert store.musical_profiles.source_input(key, observation_ids=references)[0]['observations'] == before


def test_conflict_candidates_are_read_from_files_and_resolution_can_reopen(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    store.enrichment.apply_provider_fields(track, 'a', {'genre': 'Jazz'}, providers=('a','b'))
    candidates = [{'provider':'a','value':'Jazz'},{'provider':'b','value':'Rock'}]
    store.enrichment.upsert_conflict(track, 'genre', candidates)
    assert store.conn.execute('SELECT candidates FROM enrichment_conflict').fetchone()[0] == '[]'
    detail = store.enrichment_reports.enrichment_conflict_detail(track, 'genre')
    assert {option['value'] for option in detail['options']} == {'Jazz','Rock'}
    assert store.enrichment.resolve_enrichment_candidate(track, 'genre', 'Rock')
    assert store.conn.execute('SELECT resolved FROM enrichment_conflict').fetchone()[0] == 1
    store.enrichment.upsert_conflict(track, 'genre', [{'provider':'a','value':'Blues'},{'provider':'b','value':'Rock'}])
    assert store.conn.execute('SELECT resolved FROM enrichment_conflict').fetchone()[0] == 0
    store.enrichment.retire_agreed_conflicts(track, {'genre': {'a':'Rock','b':'Rock'}})
    assert store.conn.execute('SELECT resolved FROM enrichment_conflict').fetchone()[0] == 2


def test_reopen_and_file_backup_restore_use_derived_paths(runtime_store, tmp_path):
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'genre', 'Jazz')
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path / 'backup-files')
    target = Store(tmp_path / 'moved.db')
    try:
        store.conn.backup(target.conn)
        files.restore(target.conn, manifest)
        target.init_schema()
        assert target.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (track,)).fetchone()[0] == 'Jazz'
        assert metadata_provenance(target, track)['decisions'][0]['source'] == 'user'
        for _, sql in runtime.definitions():
            assert 'filename' not in sql and 'digest' not in sql
    finally:
        target.conn.close()


@pytest.mark.parametrize('damage', (
    'owner', 'kind', 'revision', 'envelope_checksum', 'transport_checksum',
    'size', 'incomplete', 'missing_file', 'duplicate', 'unexpected',
))
def test_rejected_backup_preserves_all_live_metadata_bytes(runtime_store, tmp_path, damage):
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', 120)
        catalog.choose_field(store.conn, track, 'genre', 'Jazz')
    decisions = metadata_provenance(store, track)['decisions']
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path/'backup')
    document = json.loads(fao.read_bytes(manifest))
    paths = [files.relative(member['kind'], member['owner'], member['revision'])
             for member in document['entries']]
    before = {path: (files.directory(store.conn)/path).read_bytes() for path in paths}
    assert len(paths) == 2
    for index, (member, path) in enumerate(zip(document['entries'], paths)):
        envelope = json.loads(fao.read_bytes(tmp_path/'backup'/path))
        if index == 0:
            # This member remains valid on its own. Rejecting a later member
            # must preserve the original retained decision, including its time.
            envelope['body']['data']['at'] += 100
        elif damage in ('owner', 'kind', 'revision'):
            envelope['body'][damage] = {'owner': [999999], 'kind': 'native', 'revision': 2}[damage]
        envelope['sha256'] = hashlib.sha256(canonical(envelope['body'])).hexdigest()
        if index == 1 and damage == 'envelope_checksum':
            envelope['sha256'] = 'wrong'
        encoded = canonical(envelope)
        fao.write_bytes(tmp_path/'backup'/path, encoded)
        member['bytes'] = len(encoded)
        member['sha256'] = hashlib.sha256(encoded).hexdigest()
    if damage == 'transport_checksum':
        document['entries'][1]['sha256'] = 'wrong'
    elif damage == 'size':
        document['entries'][1]['bytes'] += 1
    elif damage == 'incomplete':
        document['entries'].pop()
    elif damage == 'missing_file':
        (tmp_path/'backup'/paths[1]).unlink()
    elif damage == 'duplicate':
        document['entries'].append(document['entries'][0])
    elif damage == 'unexpected':
        document['entries'].append({**document['entries'][1], 'owner': [999999]})
    fao.write_bytes(manifest, canonical(document))
    with pytest.raises((ValueError, FileNotFoundError)):
        files.restore(store.conn, manifest)
    assert {path: (files.directory(store.conn)/path).read_bytes() for path in paths} == before
    assert metadata_provenance(store, track)['decisions'] == decisions


def test_restore_publishes_verified_copies_after_backup_changes(runtime_store, tmp_path, monkeypatch):
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', None)
        catalog.choose_field(store.conn, track, 'bpm', '')
    decisions = metadata_provenance(store, track)['decisions']
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path/'backup')
    members = json.loads(fao.read_bytes(manifest))['entries']
    paths = [files.relative(member['kind'], member['owner'], member['revision']) for member in members]
    root = files.directory(store.conn)
    ages = {path: fao.stat(root/path) for path in paths}
    for path in paths:
        (root/path).unlink()
    copy = fao.copy
    published = []

    def replace_backup_before_publication(source, target, **kwargs):
        if target.is_relative_to(root):
            for path in paths:
                fao.write_bytes(tmp_path/'backup'/path, b'{}')
            published.append(target)
        return copy(source, target, **kwargs)

    monkeypatch.setattr(fao, 'copy', replace_backup_before_publication)
    files.restore(store.conn, manifest)
    assert len(published) == len(paths)
    assert metadata_provenance(store, track)['decisions'] == decisions
    for path in paths:
        restored = fao.stat(root/path)
        assert restored.compressed
        assert (restored.mtime_ns, restored.ctime_ns) == (ages[path].mtime_ns, ages[path].ctime_ns)


def test_waterfall_reuses_expired_completed_providers_without_refetch(runtime_store, monkeypatch):
    from tests.test_waterfall import FakeProv, _cfg
    from yt_playlist.providers.waterfall import run_waterfall
    store = runtime_store
    track, recording = entry(store)
    now = [time.time()]
    monkeypatch.setattr(runtime.time, 'time', lambda: now[0])
    a = FakeProv('a', {'Song': {'genre':'Jazz'}})
    b = FakeProv('b', {'Song': {'genre':'Rock'}})
    configuration = _cfg(('a', True), ('b', True))
    registry = {'a': a, 'b': b}
    def run():
        current = dict(store.tracks.track_for_waterfall(track))
        return run_waterfall(store, [current], configuration, lambda event: None, registry=registry,
                             run_id=str(now[0]), now_fn=lambda: now[0], reuse_completed=True)
    run()
    assert a.calls == b.calls == ['Song']
    before = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
    now[0] += runtime.ALTERNATIVE_SECONDS+1
    store.enrichment_state.expire_provider_alternatives(now[0])
    run()
    assert a.calls == b.calls == ['Song']
    assert store.enrichment_state.enrichment_provider_state(recording)['a']['fields'] == {}
    assert store.tracks.get_track_enrichment(track)[0] == 'Jazz'
    assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == before == 0


def test_file_integrity_failure_and_backup_reader_fence(runtime_store, tmp_path):
    from yt_playlist.core import policy_documents
    store = runtime_store
    track, recording = entry(store)
    at = time.time()
    store.enrichment_state.record_enrichment_attempt('song|artist','a',{'genre':'Jazz'}, now=at, video_id='video')
    with policy_documents.retained_reader(files.directory(store.conn)):
        assert store.enrichment_state.expire_provider_alternatives(at+runtime.ALTERNATIVE_SECONDS+1) == 0
        assert runtime.alternatives(store.conn, recording, 'a', now=at)['fields']['genre'] == 'Jazz'
    with store.conn:
        catalog.choose_field(store.conn, track, 'genre', 'Jazz')
    head = store.conn.execute('SELECT id,revision FROM metadata_runtime_choices').fetchone()
    path = files.directory(store.conn)/files.relative('choices', (head['id'],), head['revision'])
    path.write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        with store.conn:
            store.conn.execute('BEGIN')
            files.export(store.conn, tmp_path/'bad-backup')


def test_partial_retry_cannot_reassign_another_items_fields(runtime_store):
    store = runtime_store
    first, recording = entry(store, 'first')
    second, _ = entry(store, 'second')
    store.tracks.associate_youtube_item('second', recording, source='verified',
        evidence={'same_recording':True}, same_recording=True, now=time.time())
    now = time.time()
    store.enrichment_state.record_enrichment_attempt('song|artist', 'a',
        {'genre':'Jazz', 'year':'1995'}, video_id='first', now=now, retry=True)
    assert store.enrichment_state.enrichment_provider_state(recording, entry_id=first)['a']['fields']['year'] == '1995'
    assert 'year' not in store.enrichment_state.enrichment_provider_state(recording, entry_id=second)['a']['fields']
    store.enrichment_state.record_enrichment_attempt('song|artist', 'a',
        {'bpm':120}, video_id='second', now=now+1, retry=True)
    current = store.enrichment_state.enrichment_provider_state(recording, entry_id=second)['a']
    assert current['fields'] == {'genre':'Jazz', 'bpm':120}
    assert current['failures'] == 2


@pytest.mark.parametrize('fields', [{'bpm':'120'}, {'bpm':True}, {'energy':2}, {'year':'1995','mb_year':'2000'}])
def test_invalid_provider_capture_does_not_mark_work_complete(runtime_store, fields):
    store = runtime_store
    entry(store)
    with pytest.raises(ValueError):
        store.enrichment_state.record_enrichment_attempt('song|artist', 'a', fields, video_id='video')
    assert store.conn.execute('SELECT count(*) FROM enrichment_provider_state').fetchone()[0] == 0
    assert not list(files.directory(store.conn).rglob('*.json'))


def test_expiry_collects_deleted_subjects_and_is_bounded(runtime_store):
    store = runtime_store
    track, recording = entry(store)
    now = time.time()
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        # Cross the 128-entry traversal budget by one document. Publication is
        # setup here; keep real durable writes while using the bounded batch API.
        for start in (0, 128):
            files.publish_many(store.conn, (
                ('alternatives', (recording, 999), float(now-revision), {'fields':{}})
                for revision in range(start, min(start+128, 129))))
    root = files.directory(store.conn)
    before = len(list(root.rglob('*.json')))
    assert before == 129
    # No SQL provider owns these files: failed publications/configuration changes
    # and deleted inputs must not leave expired alternatives indefinitely.
    with store.conn:
        removed = runtime.expire(store.conn, now=now+runtime.ALTERNATIVE_SECONDS+1)
    assert 0 < removed <= 128 < before
    for _ in range(4):
        with store.conn:
            runtime.expire(store.conn, now=now+runtime.ALTERNATIVE_SECONDS+1)
    assert not list(root.rglob('*.json'))
    assert len(store.conn._close_callbacks) <= 1


def test_relocation_restores_current_native_inputs_alternatives_and_conflicts(runtime_store, tmp_path):
    store = runtime_store
    track, recording = entry(store)
    now = time.time()
    store.enrichment_state.record_enrichment_attempt('song|artist','a',{'genre':'Jazz'}, now=now, video_id='video')
    store.enrichment.upsert_conflict(track,'genre',[{'provider':'a','value':'Jazz'},{'provider':'b','value':'Rock'}])
    store.enrichment_state.record_metadata_observations(track, 'native', [MetadataObservation('mood_party', .8,
        'recording', 'native-id', 'prediction', unit='classifier_probability')], run_id='native', now=now)
    source = store.conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
    native = metadata_native.source_values(store.conn, source)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', None)
        catalog.choose_field(store.conn, track, 'bpm', '')
    decisions = metadata_provenance(store, track)['decisions']
    store.enrichment_state.record_enrichment_attempt('song|artist', 'b', {'genre': 'Rock'},
        now=now-runtime.ALTERNATIVE_SECONDS-1, video_id='video')
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path/'backup-files')
    members = json.loads(fao.read_bytes(manifest))['entries']
    assert sum(member['kind'] == 'alternatives' for member in members) == 1
    moved = Store(tmp_path/'moved.db')
    try:
        store.conn.backup(moved.conn)
        files.restore(moved.conn, manifest)
        moved.init_schema()
        assert metadata_native.source_values(moved.conn, source) == native
        assert moved.enrichment_state.enrichment_provider_state(recording)['a']['fields'] == {'genre':'Jazz'}
        assert moved.enrichment_state.enrichment_provider_state(recording)['b']['fields'] == {}
        assert metadata_provenance(moved, track)['decisions'] == decisions
        assert moved.enrichment.resolve_enrichment_candidate(track,'genre','Rock')
        assert moved.tracks.get_track_enrichment(track)[0] == 'Rock'
        row = moved.conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?',(source,)).fetchone()
        path = files.directory(moved.conn)/files.relative('native',(source,),row[0])
        path.unlink()
        with pytest.raises(ValueError, match='Missing runtime metadata document'):
            metadata_native.source_values(moved.conn, source)
    finally:
        moved.conn.close()


@pytest.mark.parametrize('kind', ('choices', 'native', 'conflicts'))
def test_restore_requires_each_durable_metadata_kind(runtime_store, tmp_path, kind):
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', None)
        catalog.choose_field(store.conn, track, 'bpm', '')
    store.enrichment.upsert_conflict(track, 'genre', [
        {'provider': 'a', 'value': 'Jazz'}, {'provider': 'b', 'value': 'Rock'}])
    store.enrichment_state.record_metadata_observations(track, 'native', [
        MetadataObservation('mood_party', .8, 'recording', 'native-id', 'prediction',
            unit='classifier_probability')], run_id='native', now=time.time())
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = files.export(store.conn, tmp_path/'backup')
    document = json.loads(fao.read_bytes(manifest))
    paths = [files.relative(member['kind'], member['owner'], member['revision'])
             for member in document['entries']]
    before = {path: (files.directory(store.conn)/path).read_bytes() for path in paths}
    omitted = next(member for member in document['entries'] if member['kind'] == kind)
    document['entries'].remove(omitted)
    fao.write_bytes(manifest, canonical(document))
    with pytest.raises(ValueError, match='missing a required document'):
        files.restore(store.conn, manifest)
    assert {path: (files.directory(store.conn)/path).read_bytes() for path in paths} == before


def test_worker_retry_reopens_without_repeating_successful_providers(runtime_store, monkeypatch):
    from tests.test_enrichment_retries import Provider, worker, tracks
    store = runtime_store
    clock = [time.time()]
    monkeypatch.setattr(runtime.time, 'time', lambda: clock[0])
    tracks(store, 3)
    def fail(track):
        if track['title'] == 'Song 0':
            raise ValueError('malformed response')
    broken, healthy = Provider('musicbrainz', fail), Provider('deezer')
    current = worker(store, clock, [broken, healthy])
    current.drain_once(limit=3)
    assert len(healthy.calls) == 3
    path = store._db_path
    store.conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        broken.fail = None
        clock[0] += 3600
        worker(reopened, clock, [broken, healthy]).drain_once(limit=3)
        assert len(healthy.calls) == 3
        assert reopened.enrichment_reports.queue_remaining() == 0
        assert reopened.conn.execute('SELECT count(*) FROM provider_attempts').fetchone()[0] == 0
    finally:
        reopened.conn.close()


def test_recovery_and_compaction_include_derived_metadata_files(runtime_store, tmp_path):
    from yt_playlist.core.db_rebuild import job
    from tests import file_custody_support as custody
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'genre', 'Jazz')
    with store.conn:
        store.conn.execute('BEGIN')
        manifests = custody.export_files(store.conn, tmp_path/'backup.db')
        owned = job._owned_files(store.conn)
    assert 'runtime_metadata' in manifests
    assert owned['files'] >= 1
    restored = Store(tmp_path/'restored.db')
    try:
        store.conn.backup(restored.conn)
        custody.restore_files(restored.conn, manifests)
        restored.init_schema()
        assert metadata_provenance(restored, track)['decisions'][0]['source'] == 'user'
    finally:
        restored.conn.close()


def test_model_input_guards_ignore_collection_progress_but_keep_manual_choices(runtime_store):
    store = runtime_store
    track, _ = entry(store)
    with store.conn:
        catalog.choose_field(store.conn, track, 'bpm', 130)
    with store.read_snapshot() as snapshot:
        assert runtime.active(snapshot.store.conn)
        with store.conn:
            store.conn.execute('UPDATE metadata_runtime_state SET gc_cursor=gc_cursor+1 WHERE id=1')
        assert snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        pending = snapshot.store.prepared_content.pending()
        assert snapshot.store.prepared_content.capture(pending)
        assert ('metadata_runtime_choices', 'key:'+str(track)) in snapshot.store._snapshot_reads.dependencies
        with store.conn:
            catalog.choose_field(store.conn, track, 'bpm', 140)
        assert not snapshot.publish(lambda: pytest.fail('Published stale manual input'))
