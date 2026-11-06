"""Current domain exports preserve exact annotations and paid-description history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from tests import file_custody_support as custody
from tests.schema.actual_library import copy_readonly_library
from yt_playlist.core import (albums, audio_annotation_files, audio_annotations,
    album_description_files, album_description_requests, evidence_ledger, fao)
from yt_playlist.core.store import Store


def test_export_restores_all_annotation_revisions_and_paid_results_independently(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    store = Store(source)
    store.init_schema()
    version = 'audio-session-targets-1'
    with store.conn:
        store.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'recording')")
        store.conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('video',1)")
        store.conn.execute("INSERT INTO audio_vibe_runs VALUES ('run','video','Title','Artist',1)")
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
    original_paid = {row[0]: album_description_files.read(row[1], row[2]) for row in store.conn.execute(
        'SELECT id,filename,content_digest FROM album_descriptions')}
    usage = list(map(tuple, store.conn.execute('SELECT * FROM provider_usage ORDER BY id')))
    originals = {row[0]: audio_annotation_files.read(row[1]) for row in store.conn.execute(
        'SELECT id,model_output_filename FROM audio_annotations')}
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
        'annotations', 'policies', 'descriptions', 'groups', 'recommendations', 'metadata', 'archives',
        'actions', 'collections', 'samples', 'choice_evidence', 'served',
        'vibe-provenance', 'vibe-models', 'profiles', 'embeddings', 'legacy-journals', 'policy-history'}
    for domain in ('vibe-provenance', 'vibe-models', 'profiles', 'embeddings', 'legacy-journals', 'policy-history'):
        assert json.loads(fao.read(manifests[domain]).bytes) == {'format': 'fao-backup-1', 'files': []}
    assert json.loads(fao.read(manifests['choice_evidence']).bytes) == {
        'format': 'choice-evidence-1', 'pages': []}
    assert json.loads(fao.read(manifests['served']).bytes) == {
        'format': 'served-diagnostics-1', 'entries': []}
    separate = tmp_path / 'restored-original.db'
    with closing(sqlite3.connect(snapshot['snapshot'])) as backup, closing(sqlite3.connect(separate)) as target:
        assert list(backup.iterdump()) == dump
        backup.backup(target)
    for domain in ('annotations', 'policies', 'descriptions'):
        shutil.rmtree(source.with_name(source.name + '.' + domain))
    for database in (Path(snapshot['restored']), separate, readonly_copy):
        candidate = Store(database)
        try:
            if database == separate:
                custody.restore_files(candidate.conn, manifests)
            candidate.init_schema()
            actual = {row[0]: audio_annotation_files.read(row[1]) for row in candidate.conn.execute(
                'SELECT id,model_output_filename FROM audio_annotations')}
            assert actual == originals
            assert candidate.rec_ledger.policy_configuration('paid-policy') == configuration
            assert {row[0]: album_description_files.read(row[1], row[2]) for row in candidate.conn.execute(
                'SELECT id,filename,content_digest FROM album_descriptions')} == original_paid
            assert album_description_requests.result(candidate.conn, claim['request_id']) == paid
            assert list(map(tuple, candidate.conn.execute('SELECT * FROM provider_usage ORDER BY id'))) == usage
            latest = audio_annotations.latest(candidate.conn, 'run', kind='session', taxonomy_version=version)
            assert audio_annotations.read(candidate.conn, latest)['state'] == 'withdrawn'
            assert candidate.conn.execute('SELECT vibe FROM audio_annotation_vibes WHERE annotation_id=?',
                                          (first,)).fetchone()[0] == 'relax'
            for row in candidate.conn.execute('SELECT model_output_filename FROM audio_annotations'):
                assert Path(row[0]).is_relative_to(audio_annotation_files.directory(candidate.conn))
            assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            candidate.conn.close()


def test_export_failure_preserves_source_and_propagates_the_failure(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    store = Store(source)
    store.init_schema()
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    def interrupted(*_args, **_kwargs):
        raise OSError('controlled backup publication failure')
    monkeypatch.setattr(audio_annotation_files, 'publish', interrupted)
    with pytest.raises(OSError, match='controlled backup publication failure'):
        custody.snapshot_library(source, tmp_path / 'failed')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not (tmp_path / 'failed' / 'restored.db').exists()
