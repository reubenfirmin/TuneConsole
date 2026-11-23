"""Serialize SQLite calls on shared connections and close their cursors."""
from contextlib import ExitStack
from functools import wraps
import sqlite3
import threading
import weakref


def _serialized(operation):
    @wraps(operation)
    def call(self, *args, **kwargs):
        connection = self.connection if isinstance(self, sqlite3.Cursor) else self
        # SQLite's own mutex is insufficient: parameter binding can retain the
        # GIL while a stepping thread holds that mutex and calls Python. Wait
        # outside SQLite, releasing the GIL, including during fetch and close.
        with connection._sqlite_lock:
            return operation(self, *args, **kwargs)
    return call


class Cursor(sqlite3.Cursor):
    @_serialized
    def _run(self, target, *args):
        try:
            return target(*args)
        except StopIteration:
            raise
        except BaseException:
            # Connection.execute() owns its implicit cursor. Retire its SQLite
            # statement here, before an exception traceback can retain it for
            # collection on another thread using this shared connection.
            # Explicit cursors remain reusable after an unsuccessful operation.
            if getattr(self, '_implicit', False):
                try:
                    self.close()
                except sqlite3.Error:
                    # A closed connection has already retired its statements.
                    # Preserve the original operation's failure.
                    pass
            raise

    def execute(self, sql, parameters=()):
        return self._run(super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        return self._run(super().executemany, sql, parameters)

    def executescript(self, sql):
        return self._run(super().executescript, sql)

    def fetchone(self):
        return self._run(super().fetchone)

    def fetchmany(self, size=None):
        return self._run(super().fetchmany, *(() if size is None else (size,)))

    def fetchall(self):
        return self._run(super().fetchall)

    def __next__(self):
        return self._run(super().__next__)

    close = _serialized(sqlite3.Cursor.close)


class Connection(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        # Reentrant for SQLite callbacks and close callbacks using this handle.
        # This guards individual native calls; repository transaction ownership
        # still belongs to the Store lock and is not changed by this guard.
        self._sqlite_lock = threading.RLock()
        self._handle_cursors = weakref.WeakSet()
        self._handle_closed = False
        self._close_callbacks = []
        self._before_close_callbacks = []
        super().__init__(database, *args, **kwargs)

    @_serialized
    def cursor(self, factory=Cursor):
        cursor = super().cursor(factory)
        self._handle_cursors.add(cursor)
        return cursor

    def execute(self, sql, parameters=()):
        cursor = self.cursor()
        cursor._implicit = True
        return cursor.execute(sql, parameters)

    def executemany(self, sql, parameters):
        cursor = self.cursor()
        cursor._implicit = True
        return cursor.executemany(sql, parameters)

    def executescript(self, sql):
        cursor = self.cursor()
        cursor._implicit = True
        return cursor.executescript(sql)

    commit = _serialized(sqlite3.Connection.commit)
    rollback = _serialized(sqlite3.Connection.rollback)
    __enter__ = _serialized(sqlite3.Connection.__enter__)
    __exit__ = _serialized(sqlite3.Connection.__exit__)
    create_function = _serialized(sqlite3.Connection.create_function)
    create_aggregate = _serialized(sqlite3.Connection.create_aggregate)
    create_window_function = _serialized(sqlite3.Connection.create_window_function)
    create_collation = _serialized(sqlite3.Connection.create_collation)
    set_progress_handler = _serialized(sqlite3.Connection.set_progress_handler)
    set_authorizer = _serialized(sqlite3.Connection.set_authorizer)
    set_trace_callback = _serialized(sqlite3.Connection.set_trace_callback)
    # interrupt() deliberately remains lock-free: cancellation must interrupt
    # an executing statement rather than wait for it to finish.

    def backup(self, target, **kwargs):
        connections = sorted({self, target}, key=id)
        with ExitStack() as locks:
            for connection in connections:
                if isinstance(connection, Connection):
                    locks.enter_context(connection._sqlite_lock)
            return super().backup(target, **kwargs)

    @_serialized
    def close(self):
        if self._handle_closed:
            return
        # SQL-backed reader lifetimes must retire while their connection is
        # usable. File-only cleanup remains after sqlite3_close_v2 below.
        try:
            with ExitStack() as readers:
                for callback in reversed(tuple(self._before_close_callbacks)):
                    readers.callback(callback)
        finally:
            self._before_close_callbacks.clear()
            # ExitStack attempts every cursor and file cleanup callback.
            try:
                with ExitStack() as cursors:
                    for cursor in tuple(self._handle_cursors):
                        cursors.callback(cursor.close)
            finally:
                self._handle_cursors.clear()
                super().close()
                self._handle_closed = True
                try:
                    with ExitStack() as files:
                        for callback in reversed(tuple(self._close_callbacks)):
                            files.callback(callback)
                finally:
                    self._close_callbacks.clear()


def connect(database, *args, **kwargs):
    kwargs.setdefault('factory', Connection)
    return sqlite3.connect(database, *args, **kwargs)
