"""Current domain exports preserve exact annotations and paid-description history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from tests import file_custody_support as custody
from tests import paid_request_support as album_description_requests
from tests.schema.actual_library import copy_readonly_library
from tests.store_fixture_support import consumer_schema, native_sql_calls, without_query_timing
from yt_playlist.core import (albums, audio_annotations,
    evidence_ledger, fao, schema_inspection)
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """File export and recovery do not start the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Export owns SQL and paid description files, without any genre vocabulary.
    # All three recovered Stores below still perform their actual admission.
    with consumer_schema(entries=(), snapshot_guards=False) as template:
        yield template


def test_export_restores_all_annotation_revisions_and_paid_results_independently(
        tmp_path, monkeypatch, consumer_store):
    source = tmp_path / 'source.db'
    store = consumer_store(source, snapshot_guards=False)
    version = 'audio-session-targets-1'
    with store.conn:
        store.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'recording')")
        store.conn.execute("INSERT INTO track_sources(video_id,recording_id) VALUES ('video',1)")
        store.audio_vibes.save_playback('run', 'video', 'Title', 'Artist', 1)
        first = audio_annotations.save(store.conn, 'run', [], ['relax'], version, {'opaque': ['saved output']}, 2)
        audio_annotations.save(store.conn, 'run', [], [], version, None, 3, state='withdrawn')
        album = albums.observe(store.conn, 'paid-album', title='Paid album', artist='Artist')
    configuration = {'prompt_version': 1, 'exact': 'policy é'}
    claim = album_description_requests.claim(store.conn, album, 'paid-policy', 'credential', 3, 4,
                                             configuration=configuration)
    assert claim['status'] == 'reserved'
    assert album_description_requests.authorize(store.conn, claim['request_id'], claim['token'], 'credential', 3, 5)
    paid = {'status': 'ready', 'description': 'Original paid prose. é', 'model': 'deepseek-flash',
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}}
    assert album_description_requests.finish(store.conn, claim['request_id'], claim['token'], paid, 6)
    original_paid = dict(store.conn.execute(
        "SELECT id,outcome_json FROM album_description_requests WHERE status='ready'"))
    usage_sql = 'SELECT * FROM album_description_requests WHERE usage_id IS NOT NULL ORDER BY usage_id'
    usage = list(map(tuple, store.conn.execute(usage_sql)))
    originals = audio_annotations.export_snapshot(store.conn)
    dump = list(store.conn.iterdump())
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    readonly_copy = tmp_path / 'readonly.db'
    source_evidence = source.with_name(source.name + '.evidence')
    original_mkdir = evidence_ledger._mkdir
    def forbid_source_directory_write(directory):
        assert directory != source_evidence, 'Read-only capture tried to create its source evidence directory'
        return original_mkdir(directory)
    with monkeypatch.context() as capture:
        capture.setattr(evidence_ledger, '_mkdir', forbid_source_directory_write)
        readonly_copy = copy_readonly_library(source, readonly_copy, capture, {})
    snapshot = custody.snapshot_library(source, tmp_path / 'export')
    manifests = snapshot['manifests']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert set(manifests) == {
        'policies', 'descriptions', 'groups', 'recommendations', 'metadata',
        'actions', 'collections', 'samples', 'choice_evidence', 'served',
        'vibe-provenance', 'vibe-models', 'profiles', 'embeddings', 'policy-history'}
    for domain in ('vibe-provenance', 'vibe-models', 'profiles', 'embeddings', 'policy-history'):
        assert json.loads(fao.read(manifests[domain]).bytes) == {'format': 'fao-backup-1', 'files': []}
    assert json.loads(fao.read(manifests['choice_evidence']).bytes) == {
        'format': 'choice-evidence-1', 'pages': []}
    assert json.loads(fao.read(manifests['served']).bytes) == {
        'format': 'served-diagnostics-1', 'entries': []}
    separate = tmp_path / 'restored-original.db'
    with closing(sqlite3.connect(snapshot['snapshot'])) as backup, closing(sqlite3.connect(separate)) as target:
        assert list(backup.iterdump()) == dump
        backup.backup(target)
    shutil.rmtree(source.with_name(source.name + '.policies'))
    for database in (Path(snapshot['restored']), separate, readonly_copy):
        candidate = Store(database)
        try:
            candidate.conn.execute('PRAGMA synchronous=OFF')
            if database == separate:
                custody.restore_files(candidate.conn, manifests)
            with schema_inspection.reuse_tables(candidate.conn):
                candidate.init_schema()
            assert audio_annotations.export_snapshot(candidate.conn) == originals
            assert candidate.audio_vibes.playback('run') == dict(video_id='video', title='Title', artist='Artist')
            assert candidate.rec_ledger.policy_configuration('paid-policy') == configuration
            assert dict(candidate.conn.execute(
                "SELECT id,outcome_json FROM album_description_requests WHERE status='ready'")) == original_paid
            assert album_description_requests.result(candidate.conn, claim['request_id']) == paid
            assert list(map(tuple, candidate.conn.execute(usage_sql))) == usage
            latest = audio_annotations.latest(candidate.conn, 'run', kind='session', taxonomy_version=version)
            assert latest['state'] == 'withdrawn'
            assert audio_annotations.read(candidate.conn, first)['vibes'] == ['relax']
            assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            candidate.conn.close()


def test_export_failure_preserves_source_and_propagates_the_failure(tmp_path, monkeypatch, consumer_store):
    source = tmp_path / 'source.db'
    store = consumer_store(source, snapshot_guards=False)
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    def interrupted(*_args, **_kwargs):
        raise OSError('controlled backup publication failure')
    monkeypatch.setattr(custody.PolicyFiles, 'export', interrupted)
    with pytest.raises(OSError, match='controlled backup publication failure'):
        custody.snapshot_library(source, tmp_path / 'failed')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not (tmp_path / 'failed' / 'restored.db').exists()
