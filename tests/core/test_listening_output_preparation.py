"""Listening files and selected chunk edges prepare independently of schema DDL."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import listening_outputs as preparation
from yt_playlist.core.store import Store


def chunk(number):
    raw = json.dumps({'rows': [[number]], 'columns': ['time'], 'catalog': {}}, indent=2)
    return hashlib.sha256(raw.encode()).hexdigest()[:24], raw


def seed(store):
    chunks = dict(chunk(number) for number in range(4))
    refs = [{'key': key, 'from': '2026-01-01', 'until': '2026-01-02', 'bytes': len(raw.encode())}
            for key, raw in list(chunks.items())[:3]]
    manifest = {'schema': 1, 'generation': 'a' * 24, 'timezone': 'America/Costa_Rica', 'built_at': 99, 'chunks': refs}
    documents = {'listening_analytics': json.dumps(manifest, indent=2),
                 'listening_edition': '{ "editorial": "Exact retained prose 🎵" }',
                 **{'listening_chunk:' + key: raw for key, raw in chunks.items()}}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,100)', documents.items())
    return documents


def finish(job, conn, size=2):
    for _ in range(100):
        result = job.step(conn, batch_size=size)
        if result.status == 'prepared':
            return result
    pytest.fail('Listening output fixture did not finish')


def head(conn, surface='listening_analytics'):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g '
                        'USING(surface,generation) WHERE surface=?', (surface,)).fetchone()


@pytest.mark.parametrize('installed', [79, 89], indirect=True)
def test_files_and_individually_verified_chunk_links_retain_exact_sources(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    preparation.JOB.install(conn)
    first = preparation.JOB.step(conn, batch_size=2)
    assert first.processed == 2 and head(conn) is None
    regular.migrate(conn)
    finish(preparation.JOB, conn)
    for surface, raw in original.items():
        assert preparation.read_original(head(conn, surface)) == raw
        assert conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    assert tuple(conn.execute(f'SELECT outputs,chunks FROM {preparation.TOTALS}').fetchone()) == (6, 4)
    manifest = head(conn)
    assert tuple(manifest[name] for name in ('built_at', 'schema_version', 'edition_key', 'timezone', 'references_count')) == (
        99, 1, 'a' * 24, 'America/Costa_Rica', 3)
    preparation.LINK_JOB.install(conn)
    assert preparation.LINK_JOB.step(conn, batch_size=2).processed == 2
    finish(preparation.LINK_JOB, conn)
    assert conn.execute(f'SELECT count(*) FROM {preparation.LINKS}').fetchone()[0] == 3
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='listening_output_publications'").fetchone()
    assert files.collect_orphans(conn) == 0
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
    # Any source edit invalidates both proofs, even before its new file exists.
    with conn:
        conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='listening_edition'")
    assert preparation.JOB.progress(conn).prepared_revision is None
    assert preparation.LINK_JOB.progress(conn).prepared_revision is None


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_long_manifest_batches_individual_references_and_reports_missing_chunks(installed):
    store, _ = installed
    conn = store.conn
    refs = [{'key': format(number, '024x'), 'from': '2026-01-01', 'until': '2026-01-01', 'bytes': 10}
            for number in range(600)]
    raw = json.dumps({'chunks': refs})
    store.rec_proposals.put_proposals('listening_analytics', {'chunks': refs}, 100)
    preparation.JOB.install(conn)
    prior = 0
    for _ in range(10):
        progress = preparation.JOB.step(conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long manifest did not finish')
    assert head(conn)['references_count'] == 600
    assert preparation.read_original(head(conn)) == raw
    preparation.LINK_JOB.install(conn)
    with pytest.raises(MigrationError, match='Missing selected listening chunk'):
        preparation.LINK_JOB.step(conn, batch_size=128)
    assert preparation.LINK_JOB.progress(conn).status == 'failed'
    regular.migrate(conn)
    assert store.rec_proposals.get_proposals('listening_analytics') == {'chunks': refs}


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_partial_manifest_source_replacement_and_key_rename_reconcile(installed):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    preparation.JOB.step(conn, batch_size=1)
    previous = conn.execute(f'SELECT * FROM {preparation.HEAD} WHERE surface=\'listening_analytics\'').fetchone()
    with conn:
        conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='listening_analytics'")
    finish(preparation.JOB, conn)
    assert head(conn)['references_count'] == 0
    assert preparation.read_original(previous) == original['listening_analytics']
    preparation.LINK_JOB.install(conn)
    finish(preparation.LINK_JOB, conn)
    assert not conn.execute(f'SELECT 1 FROM {preparation.LINKS}').fetchone()
    with conn:
        conn.execute("UPDATE rec_proposals SET surface='other',payload='opaque text' WHERE surface='listening_edition'")
    finish(preparation.JOB, conn)
    assert head(conn, 'listening_edition') is None
    assert tuple(conn.execute(f'SELECT outputs,chunks FROM {preparation.TOTALS}').fetchone()) == (5, 4)


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_file_io_releases_snapshot_and_fences_concurrent_source_edit(installed, monkeypatch):
    store, _ = installed
    seed(store)
    conn = store.conn
    preparation.JOB.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE rec_proposals SET payload='{}' WHERE surface='listening_analytics'")
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert preparation.JOB.step(conn, batch_size=1).input_revision == 1
        assert not conn.execute(f'SELECT 1 FROM {preparation.HEAD}').fetchone()
        finish(preparation.JOB, conn)
        assert preparation.read_original(head(conn)) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_interrupted_member_and_failed_file_work_keep_prior_checkpoint(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    original = seed(store)
    preparation.JOB.install(conn)
    checkpoint = preparation.JOB.step(conn, batch_size=1)
    copy = preparation.copy
    def interrupted(*args):
        copy(*args)
        raise KeyboardInterrupt('controlled listening member interruption')
    interrupted_job = replace(preparation.JOB, copy=interrupted)
    with pytest.raises(KeyboardInterrupt):
        interrupted_job.step(conn, batch_size=1)
    assert preparation.JOB.progress(conn) == checkpoint
    assert conn.execute(f'SELECT count(*) FROM {preparation.REFS}').fetchone()[0] == 1
    publish = files.publish
    def failed(*args):
        raise OSError('controlled listening disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled listening disk failure'):
        preparation.JOB.step(conn, batch_size=1)
    assert preparation.JOB.progress(conn).status == 'failed'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.JOB.step(reopened.conn)
        preparation.JOB.step(reopened.conn, batch_size=1, retry=True)
        finish(preparation.JOB, reopened.conn)
        assert preparation.read_original(head(reopened.conn)) == original['listening_analytics']
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [89], indirect=True)
@pytest.mark.parametrize('surface,value', [('listening_chunk:' + '0'*24, {'rows': [], 'columns': [], 'catalog': {}}),
    ('listening_analytics', {'chunks': [{'key': 'bad'}]}), ('listening_edition', [])])
def test_invalid_original_is_retained_and_fails_durably(installed, surface, value):
    store, _ = installed
    store.rec_proposals.put_proposals(surface, value, 100)
    preparation.JOB.install(store.conn)
    with pytest.raises((MigrationError, ValueError)):
        finish(preparation.JOB, store.conn)
    assert preparation.JOB.progress(store.conn).status == 'failed'
    assert store.rec_proposals.get_proposals(surface) == value
    regular.migrate(store.conn)
