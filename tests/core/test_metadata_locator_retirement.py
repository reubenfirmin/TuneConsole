"""Physical locator retirement has independent custody and format gates."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

from tests.core.test_metadata_runtime_preparation import seeded, run
from yt_playlist.core import catalog, fao, metadata_runtime_files as files, retained_copy
from yt_playlist.core.metadata_deliveries import Delivery, record_many
from yt_playlist.core.migrations import current
from yt_playlist.core.preparation import metadata_locators as retirement, metadata_runtime as preparation
from yt_playlist.core.store import Store


def schema(conn):
    return list(map(tuple, conn.execute('SELECT type,name,sql FROM sqlite_schema ORDER BY name')))


@pytest.fixture
def candidate(tmp_path):
    source, entry = seeded(tmp_path)
    progress = tmp_path / 'progress.json'
    run(source, progress)
    preparation.activate(source.conn, progress)
    history = list(map(tuple, source.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    with source.conn:
        source.conn.execute('BEGIN')
        manifest = files.export(source.conn, tmp_path / 'recovery')
    path = tmp_path / 'candidate.db'
    retained_copy.build(source._db_path, path)
    target = Store(path)
    files.restore(target.conn, manifest)
    target.init_schema()
    try:
        yield source, target, entry, Path(manifest), history
    finally:
        source.conn.close()
        target.conn.close()


def test_inactive_and_populated_formats_keep_installed_locators(tmp_path):
    store, _entry = seeded(tmp_path)
    try:
        before = schema(store.conn)
        with pytest.raises(ValueError, match='Activate verified'):
            retirement.retire(store.conn, tmp_path / 'absent.json')
        assert schema(store.conn) == before
        progress = tmp_path / 'progress.json'
        run(store, progress)
        preparation.activate(store.conn, progress)
        before = schema(store.conn)
        with pytest.raises(ValueError, match='populated installed metadata'):
            retirement.retire(store.conn, tmp_path / 'absent.json')
        assert schema(store.conn) == before
        assert 'raw_filename' in {row[1] for row in store.conn.execute('PRAGMA table_info(metadata_deliveries)')}
    finally:
        store.conn.close()


def test_retirement_removes_physical_fields_and_preserves_reopen_writes_recovery(candidate, tmp_path):
    source, target, entry, manifest, history = candidate
    original = schema(source.conn)
    decisions = target.enrichment_reports.metadata_provenance(entry)['decisions']
    detail = target.enrichment_reports.enrichment_conflict_detail(entry, 'genre')
    assert decisions and detail['options']
    result = retirement.retire(target.conn, manifest)
    assert result['verified_recovery_members'] > 0
    assert not retirement.COLUMNS.intersection(row[1] for row in target.conn.execute('PRAGMA table_info(metadata_deliveries)'))
    assert not retirement.REMOVED.intersection(row[0] for row in target.conn.execute('SELECT name FROM sqlite_schema'))
    assert list(map(tuple, target.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    assert schema(source.conn) == original
    assert retirement.retire(target.conn, manifest)['already_retired']
    with pytest.raises(ValueError, match='writes are retired'):
        with target.conn:
            target.conn.execute('BEGIN')
            record_many(target.conn, [Delivery('a', 'bad', 'lookup', None, time.time())])
    target.init_schema()
    assert target.enrichment_reports.metadata_provenance(entry)['decisions'] == decisions
    assert target.enrichment_reports.enrichment_conflict_detail(entry, 'genre') == detail
    fields = {'bpm': 145}
    captured, _ = target.enrichment_state.capture_provider_result(entry, 'continued', fields, run_id='new')
    target.enrichment.apply_provider_fields(entry, 'continued', fields, providers=('continued',), delivery_id=captured)
    target.enrichment_state.record_enrichment_attempt('song|artist', 'continued', fields,
        video_id='video', run_id='new', delivery_id=captured)
    with target.conn:
        catalog.choose_field(target.conn, entry, 'bpm', 140)
        catalog.withdraw_choice(target.conn, entry, 'bpm')
    assert target.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?', (entry,)).fetchone()[0] == 145
    # The ordinary complete recovery path must still load the retired format.
    from yt_playlist.core import policy_recovery
    report = policy_recovery.recover(target._db_path, tmp_path / 'recovered', progress=lambda _: None)
    restored = Store(Path(report['compact_database']))
    try:
        restored.init_schema()
        assert retirement.retired(restored.conn)
        assert restored.enrichment_reports.metadata_provenance(entry)['decisions']
        assert not restored.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restored.conn.close()


@pytest.mark.parametrize('definition', [
    'CREATE VIEW additional_locator_reader AS SELECT raw_filename FROM metadata_deliveries',
    'CREATE TRIGGER additional_delivery_guard BEFORE DELETE ON metadata_deliveries BEGIN SELECT 1; END',
])
def test_unreviewed_dependencies_keep_exact_installed_schema(candidate, definition):
    _source, target, _entry, manifest, _history = candidate
    with target.conn:
        target.conn.execute(definition)
    before = schema(target.conn)
    with pytest.raises(ValueError, match='new dependency audit'):
        retirement.retire(target.conn, manifest)
    assert schema(target.conn) == before
    assert not retirement.retired(target.conn)
    assert not target.conn.in_transaction


@pytest.mark.parametrize('failure', ['missing-backup', 'changed-backup', 'ddl', 'concurrent-write'])
def test_failed_retirement_keeps_exact_installed_schema_and_can_retry(candidate, monkeypatch, failure):
    _source, target, _entry, manifest, _history = candidate
    before = schema(target.conn)
    verify = files.verify_backup
    if failure in ('missing-backup', 'changed-backup'):
        encoded = fao.read_bytes(manifest)
        if failure == 'missing-backup':
            fao.delete(manifest)
        else:
            import json
            document = json.loads(encoded)
            document['entries'][0]['sha256'] = '0' * 64
            from yt_playlist.core.evidence_ledger import canonical
            fao.write_bytes(manifest, canonical(document))
    elif failure == 'ddl':
        def interrupt():
            if retirement.retired(target.conn):
                raise InterruptedError('after retirement DDL')
        monkeypatch.setattr(retirement, 'check_cancellable_work', interrupt)
    else:
        def changed(conn, path):
            result = verify(conn, path)
            with closing(sqlite3.connect(target._db_path)) as other:
                other.execute("INSERT INTO settings VALUES ('concurrent-retirement','changed')")
                other.commit()
            return result
        monkeypatch.setattr(files, 'verify_backup', changed)
    with pytest.raises((ValueError, OSError), match='verification|changed|DDL|No such file'):
        retirement.retire(target.conn, manifest)
    assert schema(target.conn) == before
    assert not retirement.retired(target.conn)
    assert not target.conn.in_transaction
    monkeypatch.undo()
    if failure in ('missing-backup', 'changed-backup'):
        fao.write_bytes(manifest, encoded)
    assert retirement.retire(target.conn, manifest)['retired']
