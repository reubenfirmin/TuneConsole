"""Exercise startup and offline recovery on the released SQLite layout."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.release_schema_support import release_schema
from yt_playlist.core import collection_recovery, fao
from yt_playlist.core.migrations import regular
from yt_playlist.core.migrations.recommendation_chunk_receipts import POOL_LOOKUP_INDEX
from yt_playlist.core.store import Store


@pytest.mark.parametrize('with_documents', [False, True])
def test_offline_collection_recovery_on_released_layout(tmp_path, with_documents):
    records = {
        ('discovered_artists', 1, 'discovery-candidate'): {'because': ['An anchor artist']},
        ('road_trip_drafts', 'trip', 'road-trip-draft'): {'selection': ['a recording']},
        ('actions', 1, 'action'): {'plan': {}},
        ('action_drafts', 1, 'action-draft'): {'selection': ['a playlist']},
        ('sync_attempts', 1, 'library-sync'): {'history': 'complete'},
    } if with_documents else {}
    path = tmp_path / 'installed.db'
    copy = tmp_path / 'restored.db'
    backup = tmp_path / 'backup'
    with closing(Store(path).conn) as conn:
        with conn:
            release_schema(conn)
            if with_documents:
                _seed_recovery_documents(conn, records)
        conn.execute('BEGIN')
        try:
            manifest = collection_recovery.export(conn, backup)
            with closing(sqlite3.connect(copy)) as destination:
                conn.backup(destination)
        finally:
            conn.rollback()

    exported = set()
    for digest in json.loads(fao.read(manifest).bytes)['chunks']:
        chunk = json.loads(fao.read(backup / digest[:2] / f'{digest}.json').bytes)
        exported.update((record['table'], record['id'], record['kind']) for record in chunk['records'])
    assert exported == records.keys()

    with closing(Store(copy).conn) as conn:
        collection_recovery.restore(conn, manifest)
        for (table, identity, kind), payload in records.items():
            key = 'artist_id' if table == 'discovered_artists' else 'id'
            reference = conn.execute(f'SELECT recovery_filename,recovery_digest,recovery_bytes FROM {table} '
                                     f'WHERE {key}=?', (identity,)).fetchone()
            assert Path(reference[0]).is_relative_to(collection_recovery.directory(conn))
            assert collection_recovery.read(*reference, kind=kind, owner=identity) == payload


def _seed_recovery_documents(conn, records):
    files = {table: collection_recovery.publish(conn, kind, identity, payload)
             for (table, identity, kind), payload in records.items()}
    conn.execute("INSERT INTO artists(id,name,provisional) VALUES (1,'Candidate',0)")
    conn.execute('INSERT INTO discovered_artists(artist_id,recovery_filename,recovery_digest,recovery_bytes) '
                 'VALUES (1,?,?,?)', files['discovered_artists'])
    conn.execute('INSERT INTO road_trip_drafts(id,legacy_author_unknown,revision,state,updated_at,'
                 "recovery_filename,recovery_digest,recovery_bytes) VALUES ('trip',1,'first','ready',1,?,?,?)",
                 files['road_trip_drafts'])
    revision = 'a' * 32
    conn.execute("INSERT INTO actions(id,kind,status,revision,idempotency_key,intent_digest,"
                 "recovery_filename,recovery_digest,recovery_bytes) VALUES (1,'merge_draft','draft',?,'merge',?,?,?,?)",
                 (revision, 'b' * 64, *files['actions']))
    conn.execute("INSERT INTO action_drafts(id,signature,revision,recovery_filename,recovery_digest,recovery_bytes) "
                 "VALUES (1,'signature',?,?,?,?)", (revision, *files['action_drafts']))
    conn.execute("INSERT INTO sync_attempts(id,revision,phase,recovery_filename,recovery_digest,recovery_bytes) "
                 "VALUES (1,1,'complete',?,?,?)", files['sync_attempts'])


def test_release_upgrade_retains_rows_and_applied_history(tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    store = Store(tmp_path / 'released.db')
    with closing(store.conn) as conn:
        with conn:
            release_schema(conn)
            conn.execute("INSERT INTO settings VALUES ('retained-setting','original')")
            conn.execute("INSERT INTO recordings(identity_key,title,created_at) VALUES ('retained','Song',1)")
        numbered_history = [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')]
        regular_history = [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')]
        assert len(regular_history) < len(regular.CHANGES)

        store.init_schema()

        assert [tuple(row) for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == numbered_history
        applied = [tuple(row) for row in conn.execute('SELECT * FROM schema_changes ORDER BY version')]
        assert applied[:len(regular_history)] == regular_history
        assert len(applied) == len(regular.CHANGES)
        assert conn.execute("SELECT title FROM recordings WHERE identity_key='retained'").fetchone()[0] == 'Song'
        assert store.settings.get_setting('retained-setting') == 'original'
        assert not store.activity_paused
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        receipt_lookup = [detail for _, _, _, detail in conn.execute(
            'EXPLAIN QUERY PLAN DELETE FROM recommendation_pools WHERE id=?', (1,))]
        assert any(POOL_LOOKUP_INDEX in detail for detail in receipt_lookup)
        assert 'SCAN collection_file_writes' not in receipt_lookup
        store.settings.set_setting('continuing-write', 'accepted')
        assert store.settings.get_setting('continuing-write') == 'accepted'
