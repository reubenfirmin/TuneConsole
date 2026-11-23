"""Check the shared file address cutover on an installed library snapshot."""
from collections import Counter
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from time import monotonic

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import evidence_maintenance, metadata_runtime_files, profile_files, vibe_provenance
from yt_playlist.core.migrations import regular, shared_file_receipts
from yt_playlist.core.migrations.shared_file_receipts import REFERENCES, RETIRED
from yt_playlist.core.store import Store


_PROJECTIONS = {
    'metadata-delivery': 'contract_digest,content_digest,0,delivery_id',
    'runtime-metadata': 'json_array(kind,owner_key,revision_key,owner_id),content_digest,selected,NULL',
    'listening-evidence': 'json_array(kind,record_key),content_digest,selected,NULL',
    'profile': 'kind,lower(hex(content_digest)),0,NULL',
    'prediction-provenance': "'',lower(hex(content_digest)),0,NULL",
}


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def rows(conn, sql, arguments=()):
    return list(map(tuple, conn.execute(sql, arguments)))


def references(conn):
    result = {}
    for table, (field, _, _) in REFERENCES.items():
        key = 'source_key' if table == 'rec_musical_sources' else 'id'
        result[table] = rows(conn, f'SELECT {key},{field} FROM {table} WHERE {field} IS NOT NULL ORDER BY {key}')
    return result


def owner_roots(conn):
    return dict(conn.execute("SELECT name,rootpage FROM sqlite_schema WHERE name IN (?,?,?)", tuple(REFERENCES)))


def test_installed_file_receipts_keep_addresses_and_selection(monkeypatch):
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    with closing(sqlite3.connect(path)) as previous:
        assert previous.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        before = tables(previous)
        receipts = {kind: rows(previous, f'SELECT id,byte_size,file_status,{fields} '
                                f'FROM {table} WHERE id>0 ORDER BY id')
                    for table, (kind, fields) in zip(RETIRED, _PROJECTIONS.items(), strict=True)}
        original = rows(previous, 'SELECT id,kind,owner_id,content_digest,byte_size,file_status '
                        'FROM collection_file_writes WHERE id>0 ORDER BY id')
        adopted = references(previous)
        roots = owner_roots(previous)
        checkpoints = rows(previous, 'SELECT pending,phase,checkpoint_after,active_windows FROM evidence_file_writes WHERE id=0')
        assert adopted and receipts['runtime-metadata'] and receipts['listening-evidence']

    elapsed = {}
    start = None
    cutting_over = False

    def measure(key, callback):
        def measured(*args):
            start = monotonic()
            try:
                return callback(*args)
            finally:
                elapsed[key] = elapsed.get(key, 0) + monotonic() - start

        return measured

    changes = list(regular.CHANGES)
    change = next(change for change in changes if change.name == shared_file_receipts.NAME)
    def prepare(conn, version):
        nonlocal start
        start = monotonic()
        measure('preparation', change.prepare)(conn, version)

    def apply(conn, context):
        nonlocal cutting_over
        measure('cutover_ddl', change.apply)(conn, context)
        cutting_over = True

    monkeypatch.setattr(shared_file_receipts, '_prepare_storage',
                        measure('preparation_schema', shared_file_receipts._prepare_storage))
    monkeypatch.setattr(shared_file_receipts, '_prepare_page',
                        measure('preparation_statements', shared_file_receipts._prepare_page))
    changes[change.version - 1] = replace(change, prepare=prepare, apply=apply)
    monkeypatch.setattr(regular, 'CHANGES', tuple(changes))
    validate_references = regular._foreign_key_violation
    check_references = measure('cutover_fk_check', validate_references)
    monkeypatch.setattr(regular, '_foreign_key_violation', lambda conn, owners: (
        check_references(conn, owners) if owners == change.foreign_key_owners else validate_references(conn, owners)))

    store = Store(path)
    with closing(store.conn) as conn:
        commit = conn.commit

        def commit_and_measure():
            nonlocal cutting_over
            commit()
            if cutting_over:
                elapsed['preparation_through_cutover'] = monotonic() - start
                cutting_over = False

        monkeypatch.setattr(conn, 'commit', commit_and_measure)
        report['journal_mode'] = conn.execute('PRAGMA journal_mode').fetchone()[0]
        startup = monotonic()
        store.init_schema()
        elapsed['store_startup'] = monotonic() - startup
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        for kind, expected in receipts.items():
            assert rows(conn, 'SELECT file_address,byte_size,file_status,owner_id,content_digest,selected,delivery_id '
                        'FROM collection_file_writes WHERE kind=? ORDER BY file_address', (kind,)) == expected
        assert rows(conn, 'SELECT id,kind,owner_id,content_digest,byte_size,file_status '
                    'FROM collection_file_writes WHERE id>0 AND previous_address IS NULL ORDER BY id') == original
        assert references(conn) == adopted
        assert owner_roots(conn) == roots
        pending, phase, after_key, windows = checkpoints[0]
        assert evidence_maintenance.state(conn) == dict(pending=pending, phase=phase, checkpoint_after=after_key)
        assert json.loads(conn.execute('SELECT evidence_maintenance FROM collection_file_writes WHERE id=0')
                          .fetchone()[0])['windows'] == json.loads(windows or '{}')
        assert not conn.execute("SELECT 1 FROM settings WHERE key='listening_evidence_maintenance' "
                                "OR key GLOB 'choice_window:*'").fetchone()
        for table, kind, field in (('recordings', 'recording', 'identity_key'),
                                   ('rec_musical_sources', 'source', 'source_key')):
            key = conn.execute(f'SELECT {field} FROM {table} WHERE musical_profile_file_id IS NOT NULL LIMIT 1').fetchone()[0]
            assert json.loads(profile_files.read(conn, kind, key))
        prediction = conn.execute("SELECT id FROM predictions WHERE provenance_state='ready' "
                                  'AND provenance_file_id IS NOT NULL LIMIT 1').fetchone()[0]
        assert vibe_provenance.read(conn, prediction)
        native = conn.execute("SELECT source_key,revision FROM rec_musical_sources WHERE revision>0 LIMIT 1").fetchone()
        assert metadata_runtime_files.read(conn, 'native', (native[0],), native[1])
        evidence = conn.execute("SELECT owner_id->>1 FROM collection_file_writes WHERE kind='listening-evidence' "
                                "AND owner_id->>0='artifact' AND selected=1 LIMIT 1").fetchone()[0]
        assert store.rec_ledger.evidence().get('artifact', evidence) is not None
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()

    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert references(conn) == adopted
    report.update(tables_before=len(before), tables_after=len(after), removed=sorted(before - after),
                  elapsed_seconds={key: round(seconds, 4) for key, seconds in elapsed.items()},
                  unchanged_owner_root_pages=sorted(roots),
                  receipts={kind: dict(Counter(row[2] for row in entries)) for kind, entries in receipts.items()},
                  unchanged_shared_receipts=len(original), exact_references={table: len(values) for table, values in adopted.items()},
                  verified_profiles_and_provenance=True, verified_metadata_and_evidence=True, reopened=True, foreign_key_errors=0)
    report_file('shared-file-receipts-c182.json').write_text(json.dumps(report, indent=2) + '\n')
