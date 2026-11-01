"""Lifetime admission for database replacement, shared by app and CLI handles.

SQLite's writer lock protects copying; this separate lifetime lock protects
replacement after every participating connection is closed. Lock files remain.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import weakref
from urllib.parse import parse_qs, unquote, urlsplit

from .db_space import StorageCompacting


def path_for(database):
    text = str(database)
    if text in ('', ':memory:'):
        return None
    if text.startswith('file:'):
        uri = urlsplit(text)
        if 'mode=memory' in uri.query:
            return None
        text = unquote(uri.path)
        if text == ':memory:':
            return None
    return Path(text).resolve()


def state_path(path):
    return path.with_name(path.name + '.compaction.json')


def state(path):
    try:
        value = json.loads(state_path(path).read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('format') != 1
            or value.get('database') != str(path)
            or value.get('phase') not in ACTIVE | {'complete', 'failed'}):
        raise ValueError('Invalid database compaction recovery state')
    return value


ACTIVE = frozenset({'building', 'staging', 'closing', 'original_moved', 'installed', 'reopening', 'rollback'})
CLOSED = frozenset({'closing', 'original_moved', 'installed', 'reopening', 'rollback'})


@contextmanager
def lease(path, *, exclusive=False, timeout=1):
    from .policy_documents import _try_file_lock
    lock = path.with_name(path.name + '.handles.lock')
    with lock.open('a+b') as stream:
        deadline = time.monotonic() + timeout
        while True:
            unlock = _try_file_lock(stream, exclusive=exclusive)
            if unlock is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Database handles are still open; compaction cannot replace the database')
            time.sleep(.025)
        try:
            yield
        finally:
            unlock()


class Cursor(sqlite3.Cursor):
    def _run(self, target, *args):
        self.connection._admit()
        try:
            return target(*args)
        except sqlite3.OperationalError as error:
            if (getattr(error, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_READONLY
                    and getattr(self.connection, '_storage_compacting', False)):
                self.connection.rollback()
                raise StorageCompacting() from error
            raise

    def execute(self, sql, parameters=()):
        return self._run(super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        return self._run(super().executemany, sql, parameters)

    def executescript(self, sql):
        return self._run(super().executescript, sql)


class Connection(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        self._handle_internal = kwargs.pop('_compaction_internal',False)
        self._handle_path = path_for(database)
        self._handle_readonly = (kwargs.get('uri',False)
            and parse_qs(urlsplit(str(database)).query).get('mode')==['ro'])
        self._handle_lease = None
        self._compaction_query_only = None
        self._compaction_state_key = None
        self._compaction_state = None
        self._handle_cursors = weakref.WeakSet()
        self._handle_closed = False
        self._close_callbacks = []
        if self._handle_path is not None and not self._handle_internal:
            self._handle_lease = self._enter_handles()
        try:
            super().__init__(database, *args, **kwargs)
        except BaseException:
            if self._handle_lease is not None:
                self._handle_lease.__exit__(None, None, None)
                self._handle_lease = None
            raise

    def _enter_handles(self):
        from .activity import check_cancellable_work
        deadline = time.monotonic()+10
        while True:
            pending = state(self._handle_path)
            if not pending or pending['phase'] not in CLOSED:
                borrowed = lease(self._handle_path,timeout=0 if self._handle_readonly else 1)
                try:
                    borrowed.__enter__()
                except TimeoutError:
                    if not self._handle_readonly:
                        raise
                else:
                    try:
                        # Recheck after admission to close the cutover race.
                        pending = state(self._handle_path)
                        if not pending or pending['phase'] not in CLOSED:
                            return borrowed
                    except BaseException:
                        borrowed.__exit__(None,None,None)
                        raise
                    borrowed.__exit__(None,None,None)
            if not self._handle_readonly or time.monotonic()>=deadline:
                raise StorageCompacting()
            # Wait outside the shared lease: the compactor must be able to
            # finish replacement while a new browse request waits to open.
            check_cancellable_work()
            time.sleep(.025)

    def _admit(self):
        if self._handle_path is None or self._handle_internal:
            return
        # State is atomically replaced, including detailed reports. Recheck its
        # file identity for every statement, but do not parse the complete
        # retained-row report for every ordinary browse query after completion.
        try:
            stat = state_path(self._handle_path).stat()
            key = (stat.st_ino,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size)
        except FileNotFoundError:
            key = None
        if key != self._compaction_state_key:
            self._compaction_state = state(self._handle_path) if key is not None else None
            self._compaction_state_key = key
        pending = self._compaction_state
        if pending and pending['phase'] in CLOSED and not self._handle_readonly:
            raise StorageCompacting()
        # Already admitted mode=ro readers may finish their bounded snapshot.
        # Their lifetime leases prevent any rename until SQLite closes them.
        active = pending and pending['phase'] in ACTIVE
        if active:
            if self._compaction_query_only is None:
                self._compaction_query_only = sqlite3.Connection.execute(self, 'PRAGMA query_only').fetchone()[0]
            # mode=ro already fences persistent writes. It must retain TEMP
            # writes so supported predecessor read projections can be bound on
            # a new private reader during maintenance.
            if not self._handle_readonly:
                sqlite3.Connection.execute(self, 'PRAGMA query_only=ON')
            self._storage_compacting = True
        elif self._compaction_query_only is not None:
            sqlite3.Connection.execute(self, f'PRAGMA query_only={self._compaction_query_only}')
            self._compaction_query_only = None
            self._storage_compacting = False

    def _join_handles(self):
        """Finish the compactor's exclusive-lease loan before exposing this handle."""
        if self._handle_internal and self._handle_path is not None:
            self._handle_lease = lease(self._handle_path)
            self._handle_lease.__enter__()
            self._handle_internal = False

    def cursor(self, factory=Cursor):
        cursor = super().cursor(factory)
        self._handle_cursors.add(cursor)
        return cursor

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def close(self):
        if self._handle_closed:
            return
        # Release only after sqlite3_close_v2 has closed this connection. Active
        # cursors must be drained by their owner before the exclusive lease.
        for cursor in tuple(self._handle_cursors):
            cursor.close()
        self._handle_cursors.clear()
        super().close()
        self._handle_closed = True
        if self._handle_lease is not None:
            self._handle_lease.__exit__(None, None, None)
            self._handle_lease = None
        for callback in self._close_callbacks:
            callback()
        self._close_callbacks.clear()


def connect(database, *args, **kwargs):
    kwargs.setdefault('factory', Connection)
    return sqlite3.connect(database, *args, **kwargs)
