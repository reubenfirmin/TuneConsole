"""Explicit audit timing through transaction completion on watched connections."""
import sqlite3
import time

from yt_playlist.core.db_diagnostics import DiagnosticConnection


class WriterTimings:
    def __init__(self, *, field='max_writer_seconds'):
        self.field, self.windows, self.watched, self.started = field, [], {}, {}
        self.original = DiagnosticConnection._finish
        def finish(connection, sql, target, *args):
            try:
                return self.original(connection, sql, target, *args)
            finally:
                beginning = self.started.pop(id(connection), None) if sql in ('COMMIT', 'ROLLBACK') else None
                if beginning is not None:
                    self.windows.append(time.monotonic()-beginning)
        DiagnosticConnection._finish = finish

    def watch(self, connection):
        assert isinstance(connection, DiagnosticConnection)
        self.watched[id(connection)] = connection
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                self.started[id(connection)] = time.monotonic()
        connection.set_trace_callback(trace)

    def timed(self, callback):
        assert not self.started, 'The previous audit operation left a writer transaction open'
        self.windows.clear()
        beginning = time.monotonic()
        result = callback()
        return result, {'seconds': time.monotonic()-beginning, self.field: max(self.windows, default=0)}

    def close(self):
        DiagnosticConnection._finish = self.original
        for connection in self.watched.values():
            try:
                connection.set_trace_callback(None)
            except sqlite3.ProgrammingError:
                # An audit may have already closed a connection during cleanup.
                pass
