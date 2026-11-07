"""Session-local reuse of unchanged SQLite table structure observations."""
from contextlib import contextmanager
from contextvars import ContextVar

_SESSION = ContextVar('schema_table_inspections', default=None)


class TableInspections:
    def __init__(self, conn):
        self.conn = conn
        self.version = None
        self.tables = {}

    def refresh(self):
        # Own commits keep data_version; another connection's commit invalidates
        # every observation before validation in the next pinned transaction.
        version = self.conn.execute('PRAGMA main.data_version').fetchone()[0]
        if version != self.version:
            self.tables.clear()
            self.version = version


@contextmanager
def reuse_tables(conn):
    session = TableInspections(conn)
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)


def table(conn, name, sql, inspect):
    session = _SESSION.get()
    if session is None or session.conn is not conn:
        return inspect()
    previous = session.tables.get(name)
    if previous is not None and previous[0] == sql:
        # Column/FK metadata and constraint-owned indexes derive from this exact
        # CREATE TABLE. Added ordinary indexes do not change those constraints.
        return _copy_table(previous[1])
    result = inspect()
    session.tables[name] = (sql, _copy_table(result))
    return result


def _copy_table(observed):
    # _inspect_table returns mappings of immutable column/constraint tuples
    # and integer counts, plus an immutable pair of table flags. Copy each
    # mutable mapping without recursively copying those thousands of tuples
    # on every validation of the same table. Callers still own their mappings.
    columns, foreign_keys, unique, checks, flags, annotations, deferrals = observed
    return (columns.copy(), foreign_keys.copy(), unique.copy(), checks.copy(),
            flags, annotations.copy(), deferrals.copy())
