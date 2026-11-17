"""SQLite connections, schema transitions and bounded read connections.

Application construction and retained files are supplied by a lifecycle collaborator.
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from yt_playlist.core.activity import interrupt_query
from yt_playlist.core.db_reads import MAX_READERS, ReaderDiagnostics, read_view
from yt_playlist.core.db_snapshot import read_snapshot
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.priority import RequestPriority, ServingLock
from yt_playlist.core.read_memo import ReadMemo
from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics
from yt_playlist.repos.base import synchronized


class SQLiteDatabase:
    def __init__(self, db_path, application):
        self._db_path = None if str(db_path) == ':memory:' else Path(db_path).resolve()
        self._application = application
        application.recover(self._db_path)
        self.conn = sqlite3.connect(str(db_path), uri=True, check_same_thread=False,
                                    factory=DiagnosticConnection)
        try:
            application.attach(self)
            self._configure_connection()
            self.priority = RequestPriority()
            self._lock = ServingLock(self.priority)
            self.conn._repository_lock = self._lock
            self._evidence_state_lock = threading.Lock()
            self._analytics_readers = threading.BoundedSemaphore(MAX_READERS)
            self._background_readers = threading.BoundedSemaphore(MAX_READERS - 1)
            self._reader_diagnostics = ReaderDiagnostics()
            self._foreground_read_memo = ReadMemo()
            self.background_measurement = None
            self.maintenance_diagnostics = MaintenanceDiagnostics()
        except BaseException:
            self.conn.close()
            raise

    def _configure_connection(self):
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.set_progress_handler(interrupt_query, 1000)
        from yt_playlist.util.matching import search_squash
        self.conn.create_function('searchnorm', 1, search_squash, deterministic=True)
        from yt_playlist.util.genre_catalog import normalize
        self.conn.create_function('tastenorm', 1, normalize, deterministic=True)

    def _bind_repositories(self):
        # Reopening storage and analytics preparation can replace either the
        # handle or its serving lock. Connection-bound SQL ports share that owner.
        self.conn._repository_lock = self._lock
        self._application.bind(self)

    def _retained_readers(self):
        return self._application.readers(self)

    @contextmanager
    def read_snapshot(self, *, dependencies=None, memo=None):
        with self._retained_readers():
            with read_snapshot(self, self._make_reader, dependencies=dependencies, memo=memo) as snapshot:
                yield snapshot

    @contextmanager
    def read_view(self, *, tracked=False, seconds=...):
        if getattr(self, '_read_only', False):
            yield self
            return
        with self._retained_readers():
            with read_view(self, self._make_reader, tracked=tracked, seconds=seconds) as reader:
                yield reader

    def _make_reader(self, *, bind_repositories=True):
        reader = type(self).__new__(type(self))
        reader._db_path = self._db_path
        reader._application = self._application
        reader._evidence_source = getattr(self, '_evidence_source', self)
        reader._analytics_readers = self._analytics_readers
        path = self._db_path.as_uri() + '?mode=ro' if self._db_path else ':memory:'
        reader.conn = sqlite3.connect(path, uri=self._db_path is not None,
                                      check_same_thread=False, cached_statements=0,
                                      factory=DiagnosticConnection)
        try:
            self._application.attach_reader(self, reader)
            reader._configure_connection()
            reader._lock = threading.RLock()
            reader.conn._repository_lock = reader._lock
            reader._read_only = True
            reader.maintenance_diagnostics = self.maintenance_diagnostics
            if self._db_path is not None and bind_repositories:
                reader._bind_repositories()
            return reader
        except BaseException:
            reader.conn.close()
            raise

    def memoized_read(self, name, prepare):
        calculate = getattr(self, '_memoized_read', None)
        return calculate(name, prepare) if calculate is not None else prepare()

    @classmethod
    def open_current(cls, db_path):
        """Open a validated preparation database without migrations or startup repair."""
        from yt_playlist.core.migrations import current, regular
        store = cls(db_path)
        try:
            current.validate(store.conn)
            regular.validate(store.conn)
            store._schema_compatibility = False
            store._rebuild_source_version = None
            store._schema_recovery_pending = False
            store._bind_repositories()
            if store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise RuntimeError('Domain preparation requires WAL readers')
            store._snapshot_revisions = Revisions(store.conn)
            return store
        except BaseException:
            store.conn.close()
            raise

    @synchronized
    def init_schema(self, *, defer_metadata=False):
        from yt_playlist.core.migrations import migrate
        try:
            self._schema_compatibility = bool(migrate(
                self.conn, defer_validation=defer_metadata and self._db_path is not None))
            self._rebuild_source_version = None
            self._schema_recovery_pending = False
            if ('settings' not in self.__dict__ or self._repository_schema_version !=
                    self.conn.execute('PRAGMA schema_version').fetchone()[0]):
                self._bind_repositories()
            self._application.repair(self)
            if self._db_path is not None and not self._rebuild_source_version:
                mode = self.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
                if mode.lower() != 'wal':
                    raise RuntimeError('Database requires WAL for nonblocking background readers')
            self._snapshot_revisions = Revisions(self.conn)
        except BaseException:
            self.conn.close()
            raise
