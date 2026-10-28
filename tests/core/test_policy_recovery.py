"""Offline recovery preserves source files, user data and exact served policies."""
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from yt_playlist.core import albums, policy_config, policy_recovery
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.migrations.runner import migrate
from yt_playlist.core.policy_recovery import recover
from yt_playlist.core.store import Store
from yt_playlist.rec import scorecard


def test_recovery_preserves_backup_personal_rows_active_roots_and_exact_policies(tmp_path):
    source = tmp_path / 'source.db'
    store = Store(source)
    store.init_schema()
    store.settings.set_setting('personal', 'keep forever')
    raw = {'ranker': 'old-pool', 'weights': {'genre:rock': 1.234567891}, 'evidence': 'x' * 40000}
    rows = [(f'old-{n}', json.dumps(raw), 1) for n in range(150)]
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)', rows)
    store.rec_ledger.record_served_impressions('served', 'home', [{'key': 'song', 'rank': 1,
        'model_version': 'old-0', 'provenance': {'pool_policy': 'old-1'}}], 2)
    store.rec_proposals.put_proposals('active-pool', {'nested': [{'pool_policy': 'old-2'}]}, 2)
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
    assert report['shared_bodies'] == 2
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
        assert candidate.rec_proposals.get_proposals('active-pool') == {'nested': [{'pool_policy': 'old-2'}]}
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
            store.conn.execute('INSERT INTO settings VALUES (?,?)',
                               ('active-session', '{"pool_policy":"retained"}'))
            store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,?)',
                                   [('retained', json.dumps(raw), 1), ('unused', '{}', 1)])
            albums.save(store.conn, {'browse': 'album-source', 'title': 'Album', 'artist': 'Artist'})
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
        assert candidate.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(MIGRATIONS)
        assert candidate.settings.get_setting('personal') == 'keep forever'
        assert candidate.collection.saved_album_ids() == {'album-source'}
        assert tuple(candidate.conn.execute('SELECT provider_id,is_primary FROM album_provider_ids').fetchone()) == (
            'album-source', 1)
        assert tuple(candidate.conn.execute('SELECT * FROM album_share_snapshots').fetchone()) == (
            'album-source', 'Frozen title', '["original-video"]')
        assert candidate.rec_ledger.policy_configuration('retained') == raw
        for table in ('rec_proposal_cache_builds', 'rec_proposal_cache_chunks', 'rec_proposal_cache_heads'):
            assert report['non_policy_table_counts'][table] == 0
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
