"""Connection-local revisions for optimistic publication of disposable read snapshots.

TEMP triggers follow real writes, including cascades and rollbacks. Nothing is added to the
persisted schema: these counters only need to live as long as this connection's readers. Other
connections are detected separately through SQLite's data_version.
"""
from contextlib import contextmanager
import sqlite3


_KEY_COLUMNS = {"settings": "key", "rec_proposals": "surface", "player_events": "kind",
                "recordings": "identity_key", "youtube_items": "video_id",
                "catalog_entries": "recording_id", "unresolved_items": "entry_id",
                "entry_field_choices": "entry_id", "library_memberships": "entry_id",
                "discovery_memberships": "recording_id", "discovery_sources": "recording_id",
                "rec_content_pending": "identity_key", "rec_prepared_content": "identity_key"}
# A declared catalog read consumes every recording field except enrichment's
# scheduling timestamps. Default/unannotated reads still guard the entire row.
# Excluding only these fields also guards newly added catalog columns.
_COLUMN_GROUP_EXCLUSIONS = {
    "recordings": {"catalog": {"first_enriched_at", "last_enriched_at"}},
}


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


class Revisions:
    def __init__(self, conn):
        self.conn = conn
        self.tables = set()
        self.views = {r[0] for r in conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='view'")}
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_revisions ("
                     "table_name TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,"
                     "PRIMARY KEY(table_name,record_key)) WITHOUT ROWID")
        for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'").fetchall():
            name = row[0]
            self.tables.add(name)
            columns = [r[1] for r in conn.execute(f"PRAGMA main.table_xinfo({_identifier(name)})")]
            changed = " OR ".join(f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                                  for c in columns)
            for operation in ("INSERT", "UPDATE", "DELETE"):
                keys = ["''"]
                if name in _KEY_COLUMNS:
                    keys.extend(f"'key:' || {side}.{_identifier(_KEY_COLUMNS[name])}" for side in
                                (("OLD", "NEW") if operation == "UPDATE" else
                                 ("OLD",) if operation == "DELETE" else ("NEW",)))
                writes = "".join(
                    "INSERT INTO snapshot_revisions(table_name,record_key,revision) VALUES ("
                    f"{_literal(name)},{key},1) ON CONFLICT(table_name,record_key) "
                    "DO UPDATE SET revision=revision+1;" for key in keys)
                condition = f" WHEN {changed}" if operation == "UPDATE" else ""
                trigger = _identifier(f"snapshot_revision_{name}_{operation.lower()}")
                conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {trigger} AFTER {operation} "
                             f"ON main.{_identifier(name)}{condition} BEGIN {writes} END")
                for group, excluded in _COLUMN_GROUP_EXCLUSIONS.get(name, {}).items():
                    group_changed = " OR ".join(
                        f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                        for c in columns if c not in excluded)
                    condition = f" WHEN {group_changed}" if operation == "UPDATE" else ""
                    trigger = _identifier(f"snapshot_revision_{name}_{group}_{operation.lower()}")
                    conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {trigger} AFTER {operation} "
                                 f"ON main.{_identifier(name)}{condition} BEGIN "
                                 "INSERT INTO snapshot_revisions(table_name,record_key,revision) "
                                 f"VALUES ({_literal(name)},{_literal('group:' + group)},1) "
                                 "ON CONFLICT(table_name,record_key) "
                                 "DO UPDATE SET revision=revision+1; END")
        conn.commit()

    def capture(self, dependencies=None):
        """Read all counters for snapshot validation, or seek only requested dependencies."""
        if dependencies is None:
            rows = self.conn.execute(
                "SELECT table_name,record_key,revision FROM temp.snapshot_revisions")
        else:
            dependencies = sorted(set(dependencies))
            if not dependencies:
                return {}
            values = ','.join('(?,?)' for _ in dependencies)
            rows = self.conn.execute(
                f"WITH requested(table_name,record_key) AS (VALUES {values}) "
                "SELECT r.table_name,r.record_key,r.revision FROM requested q "
                "JOIN temp.snapshot_revisions r "
                "ON r.table_name=q.table_name AND r.record_key=q.record_key",
                tuple(value for dependency in dependencies for value in dependency))
        return {(r[0], r[1]): r[2] for r in rows}


class Reads:
    def __init__(self):
        self.dependencies = set()
        self._record_keys = {}
        self._captures = []

    def include(self, dependencies):
        self.dependencies.update(dependencies)
        for captured in self._captures:
            captured.update(dependencies)

    @contextmanager
    def capture(self):
        """Capture complete dependencies, including reads already seen by an outer calculation."""
        dependencies = set()
        self._captures.append(dependencies)
        try:
            yield dependencies
        finally:
            self._captures.pop()

    @contextmanager
    def records(self, table, keys, *, prefix="key:"):
        previous = self._record_keys.get(table)
        self._record_keys[table] = tuple(prefix + key for key in keys)
        try:
            yield
        finally:
            if previous is None:
                self._record_keys.pop(table)
            else:
                self._record_keys[table] = previous

    def authorize(self, operation, table, _column, database, _trigger):
        if database == "main":
            if operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_READ:
                keys = self._record_keys.get(table)
                if keys is not None:
                    self.include({(table, key) for key in keys})
                else:
                    self.include({(table, "")})
        return sqlite3.SQLITE_OK


def setting_reads(store, keys):
    """Declare exact setting keys while preparing a read statement on a snapshot connection."""
    return keyed_reads(store, "settings", keys)


@contextmanager
def keyed_reads(store, table, keys):
    """Track exact keys; the query must constrain every consumed row to these keys."""
    if table not in _KEY_COLUMNS:
        raise ValueError(f"No keyed revision counter for {table}")
    reads = getattr(store, "_snapshot_reads", None)
    if reads is None:
        yield
    else:
        with reads.records(table, tuple(map(str, keys))):
            yield


@contextmanager
def column_group_reads(store, table, group):
    """Declare a known column group, including reads expanded through catalog views.

    SQLite authorizes every base column in a view even for SELECT identity_key,
    so column authorizer events cannot distinguish catalog inputs from timestamps.
    Only callers which do not consume the excluded fields may use this scope.
    """
    if group not in _COLUMN_GROUP_EXCLUSIONS.get(table, {}):
        raise ValueError(f"Unknown snapshot column group: {table}.{group}")
    reads = getattr(store, "_snapshot_reads", None)
    if reads is None:
        yield
    else:
        with reads.records(table, (group,), prefix="group:"):
            yield
