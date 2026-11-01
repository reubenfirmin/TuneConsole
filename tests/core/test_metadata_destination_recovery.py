"""Recovered preparation owns every retained range without its producer's files."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_metadata_destinations import stage
from tests.core.test_metadata_owner_cutover import predecessor, seed
from tests.core.test_metadata_preparation import finish
from tests.core.test_metadata_publication import publish as read_publication
from yt_playlist.core import metadata_delivery_files as files, metadata_destination_files as recovery
from yt_playlist.core import policy_documents, policy_recovery
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.preparation import metadata_destinations as destinations, metadata_evidence
from yt_playlist.core.store import Store


def prepared(store, *, unfinished=False):
    entry, recording, key = seed(store)
    metadata_evidence.JOB.install(store.conn)
    if unfinished:
        finish(store.conn)
        identity = read_publication(store.conn)
        assert not destinations.step(store.conn, identity=identity, batch_size=2).complete
    else:
        identity = stage(store.conn)
        destinations.publish(store.conn, identity=identity)
    return entry, recording, key, identity


def contents(conn):
    return {row[0]: (row[2], row[3], files.read(*row[1:])) for row in recovery.references(conn)}


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.glob('*/*.json')}


@pytest.mark.parametrize('kind', ['selected', 'unfinished', 'native-precedence'])
@pytest.mark.parametrize('original', [False, True], ids=['compact', 'verified-backup'])
def test_recovered_ranges_reads_and_continuation_survive_missing_source(predecessor, tmp_path, kind, original, monkeypatch):
    producer = predecessor
    entry, _, key, identity = prepared(producer, unfinished=kind == 'unfinished')
    if kind == 'native-precedence':
        migrate(producer.conn, registry=registry.MIGRATIONS)
        producer._bind_repositories()
    expected = producer.enrichment_state.metadata_observations(key)
    sources = {row[0]: producer.musical_profiles.source_input(row[0]) for row in producer.conn.execute(
        'SELECT source_key FROM rec_musical_sources')}
    expected_ranges = contents(producer.conn)
    assert expected and expected_ranges
    source_root = recovery.directory(producer.conn)
    before_files, before_sql = hashes(source_root), list(producer.conn.iterdump())
    history = [tuple(row) for row in producer.conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
    report = policy_recovery.recover(producer._db_path, tmp_path / 'recovered',
                                     now=10000, progress=lambda _: None)
    assert hashes(source_root) == before_files
    assert list(producer.conn.iterdump()) == before_sql
    assert 'metadata_preparation' in report['verified_backup_file_manifests']
    with closing(sqlite3.connect(report['verified_backup'])) as backup:
        assert list(backup.iterdump()) == before_sql
        if original:
            database = tmp_path / 'restored.db'
            with closing(sqlite3.connect(database)) as target:
                backup.backup(target)
        else:
            database = Path(report['compact_database'])
    assert not (tmp_path / 'recovered' / 'working.db.preparation').exists()
    candidate = Store(database)
    hidden = source_root.with_name(source_root.name + '-unavailable')
    source_root.rename(hidden)
    try:
        if original:
            policy_recovery._restore_files(candidate.conn, report['verified_backup_file_manifests'])
        candidate.init_schema(defer_metadata=True)
        assert contents(candidate.conn) == expected_ranges
        assert [tuple(row) for row in candidate.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')] == history
        assert all(Path(row[1]).is_relative_to(recovery.directory(candidate.conn))
                   for row in recovery.references(candidate.conn))
        destinations.validate_installed(candidate.conn)
        if kind == 'native-precedence':
            def wrong_owner(*args, **kwargs):
                pytest.fail('Native metadata reader fell back to prepared delivery owner')
            monkeypatch.setattr(destinations, 'native_rows', wrong_owner)
        assert candidate.enrichment_state.metadata_observations(key) == expected
        assert {source: candidate.musical_profiles.source_input(source) for source in sources} == sources
        if kind == 'unfinished':
            assert destinations.selected(candidate.conn) is None
            for _ in range(100):
                if destinations.step(candidate.conn, identity=identity, batch_size=2).complete:
                    break
            else:
                pytest.fail('Restored metadata preparation could not resume')
            destinations.publish(candidate.conn, identity=identity)
        if kind != 'native-precedence':
            assert destinations.selected(candidate.conn) == identity
        assert candidate.enrichment_state.metadata_observations(key) == expected
        assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            with candidate.conn:
                candidate.conn.execute('UPDATE ' + destinations.table('deliveries') + " SET provider='changed'")
    finally:
        candidate.conn.close()
        hidden.rename(source_root)
    assert hashes(source_root) == before_files
    assert list(producer.conn.iterdump()) == before_sql


@pytest.mark.parametrize('damage', ['missing-source', 'interrupted-export', 'interrupted-restore'])
def test_failed_recovery_never_reports_success_or_changes_source(predecessor, tmp_path, monkeypatch, damage):
    prepared(predecessor)
    conn = predecessor.conn
    before = list(conn.iterdump())
    root = recovery.directory(conn)
    before_files = hashes(root)
    hidden = None
    if damage == 'missing-source':
        source = Path(next(iter(recovery.references(conn)))[1])
        hidden = source.with_suffix('.unavailable')
        source.rename(hidden)
    else:
        def fail(*args, **kwargs):
            raise OSError('controlled metadata preparation interruption')
        monkeypatch.setattr(recovery, 'export' if damage == 'interrupted-export' else 'restore', fail)
    output = tmp_path / 'failed-recovery'
    try:
        with pytest.raises(OSError):
            policy_recovery.recover(predecessor._db_path, output, progress=lambda _: None)
        assert not (output / 'report.json').exists()
        assert list(conn.iterdump()) == before
    finally:
        if hidden is not None:
            hidden.rename(hidden.with_suffix('.json'))
    assert hashes(root) == before_files


def test_restore_rollback_restores_guard_and_all_original_references(predecessor, tmp_path, monkeypatch):
    prepared(predecessor)
    conn = predecessor.conn
    with policy_documents.retained_reader(recovery.directory(conn)):
        conn.execute('BEGIN')
        try:
            manifest = recovery.export(conn, tmp_path / 'backup')
        finally:
            conn.rollback()
    before = list(conn.iterdump())
    guard = destinations.table('deliveries_update')
    actual_check, checks = recovery.check_cancellable_work, []
    def interrupt_after_one_update():
        actual_check()
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (guard,)).fetchone():
            checks.append(True)
            if len(checks) == 2:
                raise OSError('interrupted after first owner relocation')
    monkeypatch.setattr(recovery, 'check_cancellable_work', interrupt_after_one_update)
    with pytest.raises(OSError, match='first owner relocation'):
        recovery.restore(conn, manifest)
    assert len(checks) == 2
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before
    destinations.validate_installed(conn)
    monkeypatch.setattr(recovery, 'check_cancellable_work', actual_check)
    recovery.restore(conn, manifest)
    destinations.validate_installed(conn)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
