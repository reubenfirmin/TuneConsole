"""Opt-in check that the sync consolidation preserves a populated library."""
from contextlib import closing
import json

from tests.library_image.fixtures import report_file
from yt_playlist.core import sync_state
from yt_playlist.core.migrations import regular, sync_settings
from yt_playlist.core.migrations import sync_recovery_storage as consolidation
from yt_playlist.core.store import Store


def _tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%'")}


def test_actual_sync_consolidation_preserves_resume_and_file_ownership(managed_library):
    with closing(Store(managed_library).conn) as conn:
        change = next(change for change in regular.CHANGES if change.name == consolidation.NAME)
        regular.migrate(conn, changes=regular.CHANGES[:change.version - 1])
        tables = _tables(conn)
        cursor_columns = ','.join(row[1] for row in conn.execute('PRAGMA table_info(sync_cursors)'))
        cursors = [tuple(row) for row in conn.execute(f'SELECT {cursor_columns} FROM sync_cursors ORDER BY id')]
        assert cursors
        receipt_columns = ','.join(row[1] for row in conn.execute('PRAGMA table_info(collection_file_writes)'))
        receipts = [tuple(row) for row in conn.execute(
            f'SELECT {receipt_columns} FROM collection_file_writes ORDER BY id')]
        assert receipts
        attempt_columns = ','.join(row[1] for row in conn.execute('PRAGMA table_info(sync_attempts)') if row[1] != 'id')
        attempt = tuple(conn.execute(f'SELECT {attempt_columns} FROM sync_attempts WHERE id=1').fetchone())
        epoch = conn.execute('SELECT revision FROM collection_recovery_epoch WHERE id=1').fetchone()[0]
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='collection_file_writes'").fetchone()[0]

    store = Store(managed_library)
    with closing(store.conn) as conn:
        regular.migrate(conn, changes=regular.CHANGES[:change.version])
        assert tables - _tables(conn) == {'sync_attempts', 'collection_recovery_epoch'}
        assert not _tables(conn) - tables
        assert cursors == [tuple(row) for row in conn.execute(
            f'SELECT {cursor_columns} FROM sync_cursors WHERE id>0 ORDER BY id')]
        assert receipts == [tuple(row) for row in conn.execute(
            f'SELECT {receipt_columns} FROM collection_file_writes WHERE id>0 ORDER BY id')]
        assert attempt == tuple(conn.execute(f'SELECT {attempt_columns} FROM sync_cursors WHERE id=0').fetchone())
        assert epoch == conn.execute('SELECT ownership_revision FROM collection_file_writes WHERE id=0').fetchone()[0]
        new_change = next(change for change in regular.CHANGES if change.name == sync_settings.NAME)
        regular.migrate(conn, changes=regular.CHANGES[:new_change.version - 1])
        before = _tables(conn)
        original = [dict(row) for row in conn.execute('SELECT * FROM sync_cursors WHERE id>0 ORDER BY id')]
        origin = dict(conn.execute('SELECT * FROM sync_cursors WHERE id=0').fetchone())
        regular.migrate(conn)
        assert before - _tables(conn) >= {'sync_cursors'}
        rows = sorted(sync_state.cursors(conn), key=lambda row: row['id'])
        assert [{key: row[key] for key in original[0]} for row in rows] == original
        current = sync_state.head(conn)
        assert all(current[key] == value for key, value in origin.items() if key not in sync_state.HISTORY_FIELDS)
        for name in sync_state.HISTORY_FIELDS:
            assert sync_state.worker_settings.read(conn, name) == (str(origin[name]) if origin[name] is not None else None)
        store._bind_repositories()
        previous = store.sync.state()
        assert previous['accounts']
        now = 1795276800
        revision = store.sync.begin_attempt(now)
        store.sync.checkpoint(now, previous, expected_revision=revision, previous=previous)
        assert set(store.sync.state()['accounts']) == set(previous['accounts'])
        assert conn.execute('SELECT max(id) FROM collection_file_writes').fetchone()[0] > sequence
        assert conn.execute('SELECT ownership_revision FROM collection_file_writes WHERE id=0').fetchone()[0] > epoch
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'

    report_file('tuneconsole-1434-sync-consolidation.json').write_text(json.dumps({
        'schema_change': change.version,
        'tables_before': len(tables),
        'tables_after': len(tables) - 2,
        'cursors_preserved': len(cursors),
        'file_receipts_preserved': len(receipts),
        'accounts_resumed': len(previous['accounts']),
    }, indent=2) + '\n', encoding='utf-8')
