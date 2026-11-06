"""Bind ordinary consumers to private copies of a validated empty schema.

Admission, bootstrap, upgrade and repair tests construct or reopen real Stores.
Consumer fixtures reuse the immutable schema admitted by the session builder;
they retain real repositories, revision guards, repair and WAL configuration.
"""
from copy import deepcopy
import sqlite3

from yt_playlist.core import repair
from yt_playlist.core.db_revisions import Revisions


class PristineSchema(sqlite3.Connection):
    """Keep genuinely generated connection-local guards beside the SQL seed."""

    def capture_revisions(self, revisions):
        assert not revisions.conn.execute('SELECT 1 FROM temp.snapshot_revisions').fetchone()
        statements = [sql.replace('CREATE ', 'CREATE TEMP ', 1) for sql, in
                      revisions.conn.execute(
                          "SELECT sql FROM sqlite_temp_schema WHERE sql IS NOT NULL "
                          "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name")]
        self._revision_script = 'BEGIN;\n' + ';\n'.join(statements) + ';\nCOMMIT;'
        self._revision_state = deepcopy({name: value for name, value in vars(revisions).items()
                                         if name != 'conn'})

    def bind_revisions(self, conn):
        # SQLite backup excludes TEMP. Install the original generated SQL in a
        # new private TEMP namespace, avoiding its repeated Python construction.
        assert not conn.in_transaction
        conn.executescript(self._revision_script)
        revisions = Revisions.__new__(Revisions)
        vars(revisions).update(deepcopy(self._revision_state))
        revisions.conn = conn
        revisions.schema_version = conn.execute('PRAGMA main.schema_version').fetchone()[0]
        return revisions


def initialize_consumer(store, pristine):
    assert pristine.execute('PRAGMA query_only').fetchone()[0] == 1
    pristine.backup(store.conn)
    store._schema_compatibility = False
    store._rebuild_source_version = None
    store._schema_recovery_pending = False
    store._bind_repositories()
    repair.run_once(store)
    if store._db_path is not None:
        mode = store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        assert mode.lower() == 'wal'
    store._snapshot_revisions = pristine.bind_revisions(store.conn)
