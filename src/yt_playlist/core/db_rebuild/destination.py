"""Create the current schema once and import an explicitly admitted source.

The fresh destination has its own bootstrap history. The source's exact applied
history is retained separately as provenance; it is never advanced in place or
presented as a historical migration replay.
"""
from contextlib import closing
from functools import partial
import hashlib
from pathlib import Path
import sqlite3

from ..migrations import current, regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import Context
from ..migrations.v103_entity_names import COLUMNS
from . import development, source
from .build import Batch, Destination, Stage, _configure, quote


FORMAT = 1
PROVENANCE = ('CREATE TABLE database_rebuild_source_history ('
              'version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')
REGULAR_PROVENANCE = ('CREATE TABLE database_rebuild_source_changes ('
                      'version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')
TRIGGERS = '_database_rebuild_triggers'


def signature():
    """Fence the DDL, conversion code and file-owner code across resumes."""
    root = Path(__file__).resolve().parents[1]
    result = hashlib.sha256(str(FORMAT).encode())
    for path in sorted([*root.rglob('*.py'),*root.rglob('*.sql')]):
        result.update(str(path.relative_to(root)).encode())
        result.update(path.read_bytes())
    return 'normalized-'+str(current.VERSION)+'-'+result.hexdigest()


def initialize(target):
    target.row_factory = sqlite3.Row
    current.bootstrap(target,MIGRATIONS,0.)
    target.execute(regular.HISTORY_SQL)
    for change in regular.CHANGES:
        change.apply(target,Context(0.))
        change.validate(target)
        target.execute('INSERT INTO schema_changes VALUES (?,?,?)',(change.version,change.name,0.))
    # Copy captured values without replaying live invalidation/capture effects.
    # Cross-owner domain checks run before the canonical guards are restored.
    target.execute('CREATE TABLE '+TRIGGERS+'(name TEXT PRIMARY KEY,sql TEXT NOT NULL) WITHOUT ROWID')
    for name,sql in target.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger'").fetchall():
        target.execute('INSERT INTO '+TRIGGERS+' VALUES (?,?)',(name,sql))
        target.execute('DROP TRIGGER '+quote(name))
    target.execute(PROVENANCE)
    target.execute(REGULAR_PROVENANCE)


def _mapping(reader, table, target_columns):
    mapping = dict(COLUMNS.get(table,{}))
    if table == 'quarantined_playlists':
        mapping['ytm'] = 'playlist_scope_id'
    names = {name:mapping.get(name,name) for name in source.columns(reader,table)}
    if set(names.values()) != set(target_columns):
        # The release owner gained an empty current-publication pointer. Its
        # legacy track rows are converted by the album owner in a later stage.
        if table == 'albums' and set(target_columns)-set(names.values()) == {'current_tracklist_id'}:
            return names
        raise ValueError('Missing direct column mapping: '+table)
    return names


def _copy(reader, target, cursor, limit, *, table, destination, mapping):
    values, bookmark, done = source.scan(reader,table,cursor,limit)
    if cursor is None:
        target.execute('DELETE FROM '+quote(destination))
    fields = ','.join(map(quote,mapping.values()))
    query = 'INSERT INTO '+quote(destination)+'('+fields+') VALUES ('+','.join('?' for _ in mapping)+')'
    target.executemany(query,[tuple(row[name] for name in mapping) for row in values])
    return Batch(bookmark,len(values),done)


def _digest(conn, table, fields):
    # Verification streams in source key order. Destination tables may have a
    # different physical column order, so compare explicitly mapped values.
    result,count = hashlib.sha256(),0
    cursor = None
    while True:
        values,cursor,done = source.scan(conn,table,cursor,256)
        for row in values:
            result.update(repr(tuple(row[name] for name in fields)).encode())
            result.update(b'\n')
        count += len(values)
        if done:
            return count,result.digest()


def finish(reader, target, cursor, limit):
    """Restore a bounded set of guards, then remove private construction DDL."""
    values = target.execute('SELECT name,sql FROM '+TRIGGERS+' WHERE name>? ORDER BY name LIMIT ?',
                            (cursor or '',limit)).fetchall()
    for name,sql in values:
        existing = target.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(name,)).fetchone()
        if existing is None:
            target.execute(sql)
        elif current._tokens(existing[0]) != current._tokens(sql):
            raise ValueError('A domain writer changed the destination guard: '+name)
    if len(values)<limit:
        from ..migrations import v107_legacy_pending_deletions
        v107_legacy_pending_deletions.apply(target,Context(0.))
        # Preserve all retained auto-increment high-water marks, even when the
        # largest row was removed before the source snapshot.
        if reader.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            for table,highwater in reader.execute('SELECT name,seq FROM sqlite_sequence'):
                name = 'accounts' if table=='identities' else table
                definition = target.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
                if definition and 'AUTOINCREMENT' in definition[0].upper():
                    if target.execute('SELECT 1 FROM sqlite_sequence WHERE name=?',(name,)).fetchone():
                        target.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?',(highwater,name))
                    else:
                        target.execute('INSERT INTO sqlite_sequence VALUES (?,?)',(name,highwater))
        target.execute('DROP TABLE '+TRIGGERS)
        return Batch(rows=len(values),done=True)
    return Batch(values[-1][0],len(values),False)


def for_source(path):
    """Select the isolated development importer; unsupported inputs fail closed.

    Production admission is defined alongside this importer, but selection must
    wait for its domain mappings rather than silently applying a partial copy.
    """
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        version = source.admit(reader)
        if version != source.DEVELOPMENT_VERSION:
            raise ValueError('The production schema-64 destination mappings are not installed')
        version_signature = signature()
        with closing(sqlite3.connect(':memory:')) as empty:
            _configure(empty)
            empty.execute('BEGIN')
            initialize(empty)
            target_tables = source.tables(empty)
            originals = source.tables(reader)
            unknown = originals-target_tables-development.CONVERTED-development.DERIVED-{'identities'}
            if unknown:
                raise ValueError('Unclassified source owners: '+', '.join(sorted(unknown)))
            copies = []
            for table in sorted(originals-development.CONVERTED-development.DERIVED):
                destination = {'identities':'accounts','schema_migrations':'database_rebuild_source_history',
                               'schema_changes':'database_rebuild_source_changes'}.get(table,table)
                if destination not in target_tables:
                    raise ValueError('Missing direct destination owner: '+table)
                mapping = _mapping(reader,table,source.columns(empty,destination))
                copies.append((table,destination,mapping))
            empty.rollback()

    def prepare(target):
        if signature()!=version_signature:
            raise ValueError('Destination code changed before initialization')
        initialize(target)

    def verify(reader, target):
        if signature()!=version_signature:
            raise ValueError('Destination code changed during construction')
        current.validate(target)
        regular.validate(target)
        for table,destination,mapping in copies:
            if _digest(reader,table,mapping) != _digest(target,destination,mapping.values()):
                raise ValueError('Direct rebuild changed retained values: '+table)
        # No captured event may disappear or acquire a guessed membership.
        for table in ('play_events','listening_observations','actions'):
            expected = reader.execute('SELECT count(*) FROM '+table).fetchone()[0]
            actual = target.execute('SELECT count(*) FROM '+table).fetchone()[0]
            if expected != actual:
                raise ValueError('Direct rebuild changed captured history count: '+table)
        if target.execute('SELECT 1 FROM play_events WHERE membership_ref_id IS NOT NULL LIMIT 1').fetchone():
            raise ValueError('Direct rebuild invented an uncaptured listening membership')

    stages = tuple(Stage('copy:'+table,partial(_copy,table=table,destination=destination,mapping=mapping))
                   for table,destination,mapping in copies)
    def configure(reader,target,cursor,limit):
        reader.row_factory = target.row_factory = sqlite3.Row
        return Batch(done=True)
    return Destination(version_signature,prepare,(Stage('source',configure),*stages,*development.stages(),
                                                  Stage('guards',finish)),verify)
