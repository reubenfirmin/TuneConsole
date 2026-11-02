"""Offline recovery preserves source files, user data and exact served policies."""
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
import shutil

import pytest

from yt_playlist.core import (albums, audio_annotation_files, audio_annotations, policy_config, policy_recovery,
                              album_description_files, album_description_requests, fao)
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import migrate
from yt_playlist.core.policy_recovery import recover
from yt_playlist.core.store import Store
from yt_playlist.rec import scorecard


def test_recovery_preserves_backup_personal_rows_active_roots_and_exact_policies(tmp_path):
    source = tmp_path / 'source.db'
    store = Store(source)
    # The verified backup must retain the genuine pre-relocation SQL rows.
    migrate(store.conn, registry=MIGRATIONS[:69])
    store._bind_repositories()
    store.settings.set_setting('personal', 'keep forever')
    raw = {'ranker': 'old-pool', 'weights': {'genre:rock': 1.234567891}, 'evidence': 'x' * 40000}
    rows = [(f'old-{n}', json.dumps(raw), 1) for n in range(150)]
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)', rows)
    store.rec_ledger.record_served_impressions('served', 'home', [{'key': 'song', 'rank': 1,
        'model_version': 'old-0', 'provenance': {'pool_policy': 'old-1'}}], 2)
    store.rec_proposals.put_proposals('active-pool', {'tracks': [{'pool_policy': 'old-2'}]}, 2)
    store.settings.set_setting('current-session', '{"pool_policy":"old-3"}')
    new, base = policy_config.pack({'ranker': 'recent', 'weights': {}}, 9999)
    store.rec_ledger.record_prepared_policies([new], [base])
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    report = recover(source, tmp_path / 'recovered', now=10000, progress=lambda message: None)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert report['policies_before'] == 151
    assert report['policies_deleted'] == 146
    assert report['policies_retained'] == 5
    # Original standalone policies retain exact documents; only the recent
    # explicitly shared variant has a body owner.
    assert report['shared_bodies'] == 1
    assert report['compact_bytes'] < report['original_bytes'] / 2
    assert report['source_replaced'] is False
    assert report['existing_missing_impression_references'] == {'direct': 0, 'pool': 0}
    assert not (tmp_path / 'recovered' / 'working.db').exists()
    with closing(sqlite3.connect(report['verified_backup'])) as conn:
        assert conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 151
        assert json.loads(conn.execute('SELECT configuration FROM rec_serving_policies '
                                     "WHERE model_version='old-1'").fetchone()[0]) == raw
    candidate = Store(report['compact_database'])
    candidate.init_schema()
    try:
        assert candidate.settings.get_setting('personal') == 'keep forever'
        assert candidate.rec_proposals.get_proposals('active-pool') == {'tracks': [{'pool_policy': 'old-2'}]}
        impression = candidate.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0]
        trace = scorecard.served_recommendation(candidate, impression)
        assert trace['configuration'] == trace['pool_configuration'] == raw
    finally:
        candidate.conn.close()


def test_recovery_refuses_existing_destination_and_keeps_original(tmp_path):
    store = Store(tmp_path / 'source.db')
    store.init_schema()
    store.conn.close()
    output = tmp_path / 'already-exists'
    output.mkdir()
    with pytest.raises(FileExistsError):
        recover(tmp_path / 'source.db', output)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('version', [52, 61, 62])
def test_recovery_upgrades_predecessor_without_changing_source_or_losing_data(tmp_path, version):
    source = tmp_path / 'predecessor.db'
    store = Store(source)
    raw = {'ranker': 'retained', 'weights': {'genre:rock': 1.234567891}}
    try:
        migrate(store.conn, registry=MIGRATIONS[:version])
        with store.conn:
            store.conn.execute('INSERT INTO settings VALUES (?,?)', ('personal', 'keep forever'))
            store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)',
                                   [('retained', json.dumps(raw), 1), ('unused', '{}', 1)])
            store.conn.execute('INSERT INTO rec_served_impressions('
                'request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
                "VALUES ('retained-request','home','','unresolved-subject',1,'retained','{}',2)")
            # Seed the actual predecessor vocabulary; the current writer uses
            # later canonical account and album owners.
            album = albums.observe(store.conn, 'album-source', title='Album', artist='Artist')
            store.conn.execute("INSERT INTO album_library_memberships(album_id,identity_id,source) "
                               "VALUES (?,NULL,'youtube')", (album,))
            store.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)',
                               ('album-source', 'Frozen title', '["original-video"]'))
        original = list(store.conn.iterdump())
    finally:
        store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    output = tmp_path / 'recovered'
    report = recover(source, output, now=10000, progress=lambda message: None)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert json.loads((output / 'report.json').read_text()) == report
    assert not (output / 'working.db').exists()
    assert report['policies_before'] == 2
    assert report['policies_deleted'] == report['policies_retained'] == 1
    with closing(sqlite3.connect(report['verified_backup'])) as conn:
        assert list(conn.iterdump()) == original
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    candidate = Store(report['compact_database'])
    try:
        candidate.init_schema()
        assert candidate.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        assert candidate.settings.get_setting('personal') == 'keep forever'
        assert candidate.collection.saved_album_ids() == {'album-source'}
        assert tuple(candidate.conn.execute('SELECT provider_id,is_primary FROM album_provider_ids').fetchone()) == (
            'album-source', 1)
        assert tuple(candidate.conn.execute('SELECT browse_id,title,video_ids_json FROM album_share_snapshots').fetchone()) == (
            'album-source', 'Frozen title', '["original-video"]')
        assert candidate.rec_ledger.policy_configuration('retained') == raw
        for table in ('recommendation_generations', 'recommendation_pools', 'recommendation_heads'):
            assert report['non_policy_table_counts'][table] == 0
        assert not candidate.conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'rec_proposal_cache_%'").fetchone()
        assert candidate.conn.execute('PRAGMA foreign_key_check').fetchone() is None
    finally:
        candidate.conn.close()


def test_recovery_still_rejects_non_policy_row_loss_during_cleanup(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    store = Store(source)
    store.init_schema()
    store.settings.set_setting('personal', 'keep forever')
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    clean = policy_recovery._clean_copy

    def lose_personal_row(conn, now, progress):
        result = clean(conn, now, progress)
        with conn:
            conn.execute("DELETE FROM settings WHERE key='personal'")
        return result

    monkeypatch.setattr(policy_recovery, '_clean_copy', lose_personal_row)
    output = tmp_path / 'recovered'
    with pytest.raises(ValueError, match='non-policy table counts'):
        recover(source, output, progress=lambda message: None)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not (output / 'report.json').exists()
    with closing(sqlite3.connect(output / 'verified-original.db')) as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='personal'").fetchone()[0] == 'keep forever'


def test_recovery_bundles_all_annotation_revisions_and_candidate_has_independent_files(tmp_path):
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
    output = tmp_path / 'recovered'
    report = recover(source, output, now=10000, progress=lambda message: None)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert set(report['verified_backup_file_manifests']) == {
        'annotations', 'policies', 'descriptions', 'groups', 'recommendations', 'metadata', 'archives',
        'actions', 'collections', 'samples', 'choice_evidence', 'served',
        'vibe-provenance', 'profiles', 'embeddings', 'legacy-journals', 'policy-history'}
    # These new file domains are supported but have no owners in this fixture.
    # Prove their manifests are empty rather than accepting arbitrary additions.
    for domain in ('vibe-provenance', 'profiles', 'embeddings', 'legacy-journals', 'policy-history'):
        assert json.loads(fao.read_bytes(report['verified_backup_file_manifests'][domain])) == {
            'format': 'fao-backup-1', 'files': []}
    assert json.loads(fao.read_bytes(report['verified_backup_file_manifests']['choice_evidence'])) == {
        'format': 'choice-evidence-1', 'pages': []}
    assert json.loads(fao.read_bytes(report['verified_backup_file_manifests']['served'])) == {
        'format': 'served-diagnostics-1', 'entries': []}
    with closing(sqlite3.connect(report['verified_backup'])) as backup:
        assert list(backup.iterdump()) == dump
        # Restoring the untouched original is an explicit operation on a new copy.
        with closing(sqlite3.connect(tmp_path / 'restored-original.db')) as restored:
            backup.backup(restored)
    shutil.rmtree(source.with_name(source.name + '.annotations'))
    shutil.rmtree(source.with_name(source.name + '.policies'))
    shutil.rmtree(source.with_name(source.name + '.descriptions'))
    assert not (output / 'working.db.annotations').exists()
    for database in (Path(report['compact_database']), tmp_path / 'restored-original.db'):
        candidate = Store(database)
        try:
            if database.name == 'restored-original.db':
                policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
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


def test_recovery_file_export_failure_leaves_original_and_no_success_report(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    store = Store(source)
    store.init_schema()
    store.conn.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    def interrupted(*_args, **_kwargs):
        raise OSError('controlled backup publication failure')
    monkeypatch.setattr(audio_annotation_files, 'publish', interrupted)
    output = tmp_path / 'recovered'
    with pytest.raises(OSError, match='controlled backup publication failure'):
        recover(source, output, progress=lambda message: None)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert not (output / 'report.json').exists()


def test_saved_database_recovery(request, tmp_path):
    source = request.config.getoption('policy_recovery_source')
    if not source:
        pytest.skip('Pass --policy-recovery-source to rehearse against a saved database')
    output = request.config.getoption('policy_recovery_output') or tmp_path / 'recovery'
    report = recover(source, output)
    assert report['compact_bytes'] < report['original_bytes']
    assert report['retained_policy_fingerprints'] == 'identical'
    candidate = Store(report['compact_database'])
    candidate.init_schema()
    try:
        assert candidate.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == (
            report['non_policy_table_counts']['rec_served_impressions'])
        sample = candidate.conn.execute('SELECT id FROM rec_served_impressions ORDER BY id DESC LIMIT 1').fetchone()
        if sample:
            assert scorecard.served_recommendation(candidate, sample[0])['configuration'] is not None
    finally:
        candidate.conn.close()
