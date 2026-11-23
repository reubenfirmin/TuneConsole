"""Copy track sources and their dependent rows for the C74 startup upgrade."""
from dataclasses import dataclass
import re

from .. import library_scope_changes, recording_metadata, source_metadata, track_sources
from ..schema_contract import MigrationError
from .control_settings_storage import predecessor_metadata_active
from .runner import PreparationChanged


PREFIX = '_source_fold_'
PROGRESS = PREFIX + 'progress'
CREDITS = PREFIX + 'recording_additional_artists'
PAGE_SIZE = 500
_IDENTIFIER = r'(?:"\w+"|\w+)'
_KEY_COLUMNS = rf'\(\s*{_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*\s*\)'
_FOREIGN_KEY = re.compile(
    rf'(?:,\s*FOREIGN KEY\s*{_KEY_COLUMNS})?\s+REFERENCES\s+{_IDENTIFIER}\s*{_KEY_COLUMNS}'
    r'(?:\s+ON DELETE CASCADE)?(?:\s+DEFERRABLE INITIALLY DEFERRED)?',
    re.I,
)


def _tuple(columns, prefix=''):
    return '(' + ','.join(f'{prefix}"{column}"' for column in columns) + ')'


@dataclass(frozen=True)
class Copy:
    name: str
    sql: str
    version_column: str | None = None

    @property
    def stage(self):
        return PREFIX + self.name

    @property
    def source(self):
        return 'catalog_entries' if self.name == 'track_sources' else self.name

    def keys(self, conn):
        if self.name == 'track_sources':
            return ('id',)
        if conn.execute(f"SELECT wr FROM pragma_table_list('{self.source}') WHERE schema='main'").fetchone()[0]:
            return tuple(row[0] for row in conn.execute(
                f'SELECT name FROM pragma_table_xinfo("{self.source}") WHERE pk>0 ORDER BY pk'))
        primary = _rowid_alias(conn, self.source)
        return (primary or 'rowid',)


def _rowid_alias(conn, table):
    primary = [row for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]]
    if len(primary) == 1 and primary[0][2].upper() == 'INTEGER' and not conn.execute(
            f'SELECT 1 FROM pragma_index_list("{table}") WHERE origin=\'pk\'').fetchone():
        return primary[0][1]
    return None


def stage_definition(table, sql):
    # Prepared rows can refer to tables that take their final names at cutover.
    sql = _FOREIGN_KEY.sub('', sql)
    return re.sub(r'^CREATE TABLE "?\w+"?', f'CREATE TABLE "{table}"', sql, count=1, flags=re.I)


def require_matching_sources(conn):
    for table, link in (
        ('youtube_items', 'e.video_id=s.video_id AND e.recording_id=s.recording_id'),
        ('unresolved_items', 'e.id=s.entry_id AND e.video_id IS NULL'),
        ('library_memberships', 'e.id=s.entry_id'),
    ):
        if conn.execute(f'SELECT 1 FROM {table} s LEFT JOIN catalog_entries e ON {link} '
                        'WHERE e.id IS NULL LIMIT 1').fetchone():
            raise MigrationError('Unmatched track source reference in ' + table)


def _create(conn, copies):
    conn.execute(f'''CREATE TABLE {PROGRESS}(
        name TEXT PRIMARY KEY, complete INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID''')
    conn.execute(stage_definition(CREDITS, recording_metadata.CREDITS))
    for copy in copies:
        conn.execute('INSERT INTO ' + PROGRESS + '(name) VALUES (?)', (copy.name,))
        conn.execute(stage_definition(copy.stage, copy.sql))


def _projection(conn, copy, keys, where, args):
    from .track_source_cutover import SOURCE_FIELDS, _columns

    if copy.name == 'track_sources':
        sql = track_sources.installed_projection(columns=('e.id', 'e.recording_id', 'e.video_id', 'e.source_ref'))
        columns = SOURCE_FIELDS
        where = where.replace('original.', 'e.')
    else:
        columns = _columns(conn, copy.stage)
        if keys == ('rowid',):
            columns.insert(0, 'rowid')
        values = [f'original."{column}"' for column in columns]
        if (version := copy.version_column) is not None:
            conn.execute(f'INSERT INTO vibe_versions(value) SELECT DISTINCT original."{version}" '
                         f'FROM "{copy.source}" original WHERE {where} ON CONFLICT(value) DO NOTHING', args)
            values[columns.index(version + '_id')] = f'(SELECT id FROM vibe_versions WHERE value=original."{version}")'
        sql = f'SELECT {",".join(values)} FROM "{copy.source}" original'
    columns = ','.join(f'"{column}"' for column in columns)
    conn.execute(f'INSERT INTO "{copy.stage}"({columns}) {sql} WHERE {where}', args)
    staged_where = where.replace('e.' if copy.name == 'track_sources' else 'original.', '')
    if copy.name == 'track_sources':
        source_metadata.refresh_range(conn, table=copy.stage, where=staged_where, args=args, predecessor=True)
    if copy.name == 'recordings':
        target = f'SELECT id FROM "{copy.stage}" WHERE {staged_where}'
        conn.execute(recording_metadata._refresh(target, folded=True, preparation_table=copy.stage), args)
        for statement in recording_metadata._refresh_credits(target, recordings=copy.stage, additional=CREDITS):
            conn.execute(statement, args)


def _ordered(keys, direction='ASC'):
    return ','.join(f'"{key}" {direction}' for key in keys)


def _initial_page(conn, copy, keys):
    columns = ','.join(f'"{key}"' for key in keys)
    last = conn.execute(f'SELECT {columns} FROM "{copy.stage}" ORDER BY {_ordered(keys, "DESC")} LIMIT 1').fetchone()
    operands = '(' + ','.join('?' for _ in keys) + ')'
    after = f'WHERE {_tuple(keys)}>{operands}' if last is not None else ''
    page = conn.execute(f'SELECT {columns} FROM "{copy.source}" {after} '
                        f'ORDER BY {_ordered(keys)} LIMIT {PAGE_SIZE}', tuple(last) if last is not None else ()).fetchall()
    if not page:
        conn.execute(f'UPDATE {PROGRESS} SET complete=1 WHERE name=?', (copy.name,))
        return
    key = _tuple(keys, 'original.')
    _projection(conn, copy, keys, f'{key}>={operands} AND {key}<={operands}', (*page[0], *page[-1]))
    if copy.name == 'recordings':
        library_scope_changes.enqueue_missing(conn, page[-1][0], after=(last[0] if last is not None else 0))


def _retire_write_capture(conn, copies):
    journals = [name for name, in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB ?", (PREFIX + 'changes_*',))]
    if not journals:
        return
    runtime_active, = conn.execute(f"SELECT runtime_active FROM {PROGRESS} WHERE name='track_sources'").fetchone()
    dirty = (runtime_active != predecessor_metadata_active(conn) or
             any(conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() for name in journals))
    for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB ?",
                              (PREFIX + '*',)).fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    for name in journals:
        conn.execute(f'DROP TABLE "{name}"')
    if dirty:
        for table in (CREDITS, *(copy.stage for copy in copies)):
            conn.execute(f'DELETE FROM "{table}"')
        conn.execute(f'UPDATE {PROGRESS} SET complete=0')


def _prepare_page(conn, copies):
    if not track_sources.pending(conn):
        return False
    if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (PROGRESS,)).fetchone():
        require_matching_sources(conn)
        _create(conn, copies)
        return True
    _retire_write_capture(conn, copies)
    progress = dict(conn.execute(f'SELECT name,complete FROM {PROGRESS}'))
    for copy in copies:
        if not progress[copy.name]:
            _initial_page(conn, copy, copy.keys(conn))
            return True
    return False


def prepare(conn, copies):
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        while True:
            conn.execute('BEGIN IMMEDIATE')
            with conn:
                if not _prepare_page(conn, copies):
                    return
    finally:
        conn.execute(f'PRAGMA foreign_keys={foreign_keys}')


def require_complete(conn, copies):
    if not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (PROGRESS,)).fetchone():
        # Schema fixtures build empty layouts inside an existing transaction.
        if any(conn.execute(f'SELECT 1 FROM "{copy.source}" LIMIT 1').fetchone() for copy in copies):
            raise MigrationError('Track source preparation has not run')
        _create(conn, copies)
        conn.execute(f'UPDATE {PROGRESS} SET complete=1')
    if conn.execute(f'SELECT 1 FROM {PROGRESS} WHERE complete=0').fetchone():
        raise PreparationChanged()


def clear(conn):
    conn.execute(f'DROP TABLE {PROGRESS}')
