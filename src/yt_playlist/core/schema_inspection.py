"""Session-local reuse of unchanged SQLite table structure observations."""
from contextlib import contextmanager
from contextvars import ContextVar

_SESSION = ContextVar('schema_table_inspections', default=())
_OBJECTS = ContextVar('schema_validation_objects', default=None)


def definitions(conn, names):
    """Read named definitions from this connection's current main schema."""
    return {name: sql for name, (_kind, sql) in objects(conn, names).items()}


def objects(conn, names=None):
    snapshot = _OBJECTS.get()
    if snapshot is not None and snapshot[0] is conn:
        catalog = snapshot[1]
        if names is None:
            return catalog.copy()
        return {name: catalog[name] for name in names if name in catalog}
    return _objects(conn, names)


def catalog_rows(conn):
    """Keep every main-schema object, including names shared by tables and triggers."""
    snapshot = _OBJECTS.get()
    if snapshot is not None and snapshot[0] is conn:
        return snapshot[2]
    return tuple(conn.execute('SELECT name,type,sql FROM main.sqlite_schema').fetchall())


@contextmanager
def reuse_objects(conn):
    """Share catalog reads only during one read-only, pinned validation.

    The caller must not change schema inside this scope. Discard observations
    before any migration runs, commits or rolls back: SQLite can reuse a schema
    version after rollback, so that counter cannot identify later transactions.
    """
    snapshot = _OBJECTS.get()
    if not conn.in_transaction or snapshot is not None and snapshot[0] is conn:
        yield
        return
    rows = catalog_rows(conn)
    token = _OBJECTS.set((conn, _named_objects(rows), rows))
    try:
        yield
    finally:
        _OBJECTS.reset(token)


def _objects(conn, names=None):
    if names is None:
        return _named_objects(catalog_rows(conn))
    parameters = tuple(dict.fromkeys(names))
    if not parameters:
        return {}
    sql = 'SELECT name,type,sql FROM main.sqlite_schema WHERE name IN (' + ','.join('?' for _ in parameters) + ')'
    return _named_objects(conn.execute(sql, parameters).fetchall())


def _named_objects(rows):
    result = {}
    for name, kind, definition in rows:
        result.setdefault(name, (kind, definition))
    return result


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
    # Nested admission/worker checks belong to the caller's bounded session.
    # Transaction entry still refreshes data_version before reusing observations.
    sessions = _SESSION.get()
    session = _table_session(conn)
    if session is not None:
        yield session
        return
    session = TableInspections(conn)
    token = _SESSION.set((*sessions, session))
    try:
        yield session
    finally:
        _SESSION.reset(token)


def _table_session(conn):
    # Interleaved readers retain their own observations until their scope ends.
    return next((session for session in reversed(_SESSION.get()) if session.conn is conn), None)


def table(conn, name, sql, inspect):
    session = _table_session(conn)
    if session is None:
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
