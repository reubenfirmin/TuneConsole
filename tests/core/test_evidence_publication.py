"""Deliberate native receipt, interruption, restart and recovery checks."""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3

import pytest

from yt_playlist.core import choice_evidence_files, evidence_ledger, fao
from yt_playlist.core.evidence_paths import record_path
from yt_playlist.core.store import Store


def test_pinned_preparation_reader_publishes_context_through_the_original_writer(file_backed_store):
    from yt_playlist.rec import choice_evidence

    store = file_backed_store
    value = {'at': 100, 'saved': {'values': [0.4, 0.7]}}
    with store.read_snapshot() as snapshot:
        assert snapshot.store.conn.in_transaction
        frozen = choice_evidence.freeze_context(snapshot.store, value)
        assert snapshot.store.conn.in_transaction
        assert not store.conn.in_transaction
        assert snapshot.store.rec_ledger.evidence().get('artifact', frozen['artifact']) == value
        with closing(sqlite3.connect(store._db_path)) as observer:
            assert observer.execute("SELECT count(*) FROM collection_file_writes WHERE kind='listening-evidence' "
                                    "AND file_status='ready'").fetchone()[0]
    assert store.rec_ledger.evidence().get('artifact', frozen['artifact']) == value


def test_pending_evidence_is_hidden_after_file_and_sql_interruptions_and_reopens(file_backed_store, monkeypatch):
    store = file_backed_store
    ledger = store.rec_ledger.evidence()
    good = {'id': 'good', 'value': 'retained'}
    ledger.put('response', 'good', good)
    observed = []

    @contextmanager
    def interrupted(path, *args, **kwargs):
        assert not store.conn.in_transaction
        with closing(sqlite3.connect(store._db_path)) as observer:
            pending = observer.execute("SELECT file_address FROM collection_file_writes WHERE kind='listening-evidence' "
                                       "AND file_status='pending' ORDER BY file_address DESC").fetchone()[0]
        assert path == record_path(ledger.directory, 'response', pending)
        observed.append(pending)
        raise OSError('evidence file interrupted')
        yield

    with monkeypatch.context() as patch:
        patch.setattr(fao, 'prepare', interrupted)
        with pytest.raises(OSError, match='evidence file interrupted'):
            ledger.put('response', 'new', {'id': 'new'})
    store.conn.execute("CREATE TEMP TRIGGER reject_evidence_completion BEFORE UPDATE OF file_status ON collection_file_writes "
                       "WHEN NEW.kind='listening-evidence' AND NEW.file_status='ready' "
                       "BEGIN SELECT RAISE(ABORT,'evidence head interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='evidence head interrupted'):
        ledger.put('response', 'new', {'id': 'new'})
    store.conn.execute('DROP TRIGGER reject_evidence_completion')
    assert ledger.get('response', 'new') is None
    assert ledger.records('response') == [good]
    pending = [row[0] for row in store.conn.execute("SELECT file_address FROM collection_file_writes "
                                                   "WHERE kind='listening-evidence' AND file_status='pending' ORDER BY file_address")]
    assert len(pending) == 2
    assert record_path(ledger.directory, 'response', pending[-1]).is_file()
    store.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        retry = reopened.rec_ledger.evidence()
        assert retry.get('response', 'good') == good
        assert retry.get('response', 'new') is None
        assert retry.put('response', 'new', {'id': 'new'})
        assert int(retry._path('response', 'new').stem) > max(pending)
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        reopened.conn.close()


def test_backup_restores_native_and_installed_evidence_with_their_identities_and_clocks(file_backed_store, tmp_path):
    store = file_backed_store
    ledger = store.rec_ledger.evidence()
    installed = evidence_ledger.EvidenceLedger(ledger.directory, ledger.byte_budget)
    installed.put('response', 'old', {'id': 'old'})
    context = {'saved': {'values': [0.7]}}
    identity = ledger.artifact(context)
    ledger.put('opportunity', 'menu', {'id': 'menu', 'context': identity})
    before = {path: fao.read(path).info for path in choice_evidence_files.references(store.conn)}
    with store.conn.atomic():
        manifest = choice_evidence_files.export(store.conn, tmp_path / 'backup')
    shutil.rmtree(ledger.directory)
    choice_evidence_files.restore(store.conn, manifest)
    assert ledger.get('response', 'old') == {'id': 'old'}
    assert ledger.get('artifact', identity) == context
    assert ledger.get('opportunity', 'menu')['context'] == identity
    assert set(choice_evidence_files.references(store.conn)) == set(before)
    for path, age in before.items():
        restored = fao.read(path).info
        assert (restored.mtime_ns, restored.ctime_ns) == (age.mtime_ns, age.ctime_ns)


def test_backup_rejects_a_missing_selected_native_file(file_backed_store, tmp_path):
    store = file_backed_store
    ledger = store.rec_ledger.evidence()
    ledger.put('response', 'required', {'id': 'required'})
    ledger._path('response', 'required').unlink()
    with pytest.raises(FileNotFoundError, match='Missing SQL-owned'):
        with store.conn.atomic():
            choice_evidence_files.export(store.conn, tmp_path / 'backup')
