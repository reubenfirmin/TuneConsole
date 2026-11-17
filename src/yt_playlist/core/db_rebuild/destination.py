"""Explicit current-format cutover; ordinary upgrades use regular migrations."""
from contextlib import closing
from functools import partial
import hashlib
from pathlib import Path
import sqlite3

from .. import destination_schema, schema_bootstrap, vibe_versions
from ..migrations import current, regular
from ..migrations.registry import MIGRATIONS
from . import source, verification
from .build import Batch, Destination, Stage, _configure, quote

TRIGGERS = '_database_rebuild_triggers'
INDEXES = '_database_rebuild_indexes'


def signature():
    root = Path(__file__).resolve().parents[2]
    result = hashlib.sha256(destination_schema.DEFINITION.read_bytes())
    for path in sorted([*root.rglob('*.py'), *root.rglob('*.sql')]):
        result.update(str(path.relative_to(root)).encode())
        result.update(path.read_bytes())
    return 'current-107-' + result.hexdigest()


def initialize(target):
    from ..schema_inspection import reuse_tables
    with reuse_tables(target):
        target.row_factory = sqlite3.Row
        schema_bootstrap.bootstrap(target, MIGRATIONS, regular.CHANGES, 0., validation=current.validate)
        # Private construction suspends guards and builds secondary indexes once.
        for kind, owner in (('trigger', TRIGGERS), ('index', INDEXES)):
            target.execute('CREATE TABLE '+owner+'(name TEXT PRIMARY KEY,sql TEXT NOT NULL) WITHOUT ROWID')
            objects = target.execute('SELECT name,sql FROM sqlite_schema WHERE type=? AND sql IS NOT NULL', (kind,)).fetchall()
            for name, sql in objects:
                if kind == 'index' and sql.upper().startswith('CREATE UNIQUE'):
                    continue
                target.execute('INSERT INTO '+owner+' VALUES (?,?)', (name, sql))
                target.execute('DROP '+kind+' '+quote(name))
        # Preserve provenance already present in a supported installed database.
        for name in ('database_rebuild_source_history', 'database_rebuild_source_changes'):
            target.execute('CREATE TABLE '+name+'(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at REAL NOT NULL)')


def _mapping(reader, table, target_columns):
    original = source.columns(reader, table)
    mapping = {name: name for name in original}
    version = vibe_versions.COLUMNS.get(table)
    if version in original:
        mapping[version] = version+'_id'
    additions = set(target_columns) - set(mapping.values())
    allowed = {'albums': {'current_tracklist_id'}, 'sample_attributes': {'content_digest', 'file_status'}}
    if set(mapping.values()) - set(target_columns) or additions - allowed.get(table, set()):
        raise ValueError('Missing current column mapping: '+table)
    if source.keys(reader, table)[0] not in original:
        mapping = {'_legacy_id': source.keys(reader, table)[0], **mapping}
    return mapping


def _copy(reader, target, cursor, limit, *, table, mapping):
    if cursor is None:
        target.execute('DELETE FROM '+quote(table))
    version = vibe_versions.COLUMNS.get(table)
    if version in mapping:
        rows, bookmark, done = source.scan(reader, table, cursor, limit, legacy_id='_legacy_id' in mapping)
        target.executemany('INSERT INTO '+quote(table)+'('+','.join(map(quote, mapping.values()))
            +') VALUES ('+','.join('?' for _ in mapping)+')',
            [tuple(vibe_versions.intern(target, row[name]) if name == version else row[name]
                   for name in mapping) for row in rows])
        return Batch(bookmark, len(rows), done)
    bookmark, count, done = source.window(reader, table, cursor, limit)
    if count:
        keys = ','.join(map(quote, source.keys(reader, table)))
        marks = ','.join('?' for _ in bookmark)
        conditions, parameters = ['('+keys+')<=('+marks+')'], list(bookmark)
        if cursor is not None:
            conditions.append('('+keys+')>('+marks+')')
            parameters.extend(cursor)
        selected = ','.join(quote(source.keys(reader, table)[0] if name == '_legacy_id' else name)
                            for name in mapping)
        target.execute('INSERT INTO '+quote(table)+'('+','.join(map(quote, mapping.values()))
            +') SELECT '+selected+' FROM rebuild_source.'+quote(table)+' WHERE '
            +' AND '.join(conditions)+' ORDER BY '+keys, parameters)
    return Batch(bookmark, count, done)


def indexes(reader, target, cursor, limit):
    rows = target.execute('SELECT name,sql FROM '+INDEXES+' WHERE name>? ORDER BY name LIMIT ?',
                          (cursor or '', min(limit, 8))).fetchall()
    for _, sql in rows:
        target.execute(sql)
    if len(rows) < min(limit, 8):
        target.execute('DROP TABLE '+INDEXES)
        return Batch(rows=len(rows), done=True)
    return Batch(rows[-1][0], len(rows), False)


def finish(reader, target, cursor, limit):
    rows = target.execute('SELECT name,sql FROM '+TRIGGERS+' WHERE name>? ORDER BY name LIMIT ?',
                          (cursor or '', limit)).fetchall()
    for name, sql in rows:
        existing = target.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if existing is None:
            target.execute(sql)
        elif current._tokens(existing[0]) != current._tokens(sql):
            raise ValueError('A domain writer changed the destination guard: '+name)
    if len(rows) == limit:
        return Batch(rows[-1][0], len(rows), False)
    target.execute('UPDATE prediction_id_state SET high_water=max(high_water,'
                   '(SELECT coalesce(max(id),0) FROM predictions)) WHERE id=1')
    if reader.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        for table, highwater in reader.execute('SELECT name,seq FROM sqlite_sequence'):
            definition = target.execute('SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()
            if definition and 'AUTOINCREMENT' in definition[0].upper():
                if target.execute('SELECT 1 FROM sqlite_sequence WHERE name=?', (table,)).fetchone():
                    target.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (highwater, table))
                else:
                    target.execute('INSERT INTO sqlite_sequence VALUES (?,?)', (table, highwater))
    target.execute('DROP TABLE '+TRIGGERS)
    return Batch(rows=len(rows), done=True)


def for_source(path, *, split=False):
    """Keep current Vibes facts/files during the approved offline constraint cutover."""
    path = Path(path).resolve(strict=True)
    version_signature = signature()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        source.admit(reader)
        with closing(sqlite3.connect(':memory:')) as empty:
            _configure(empty)
            empty.execute('BEGIN')
            initialize(empty)
            originals, destinations = source.tables(reader), source.tables(empty)
            if unknown := originals - destinations:
                raise ValueError('Unclassified current source owners: '+', '.join(sorted(unknown)))
            copies = [(table, _mapping(reader, table, source.columns(empty, table)))
                      for table in sorted(originals, key=lambda name: (name != 'vibe_versions', name))]

    def initialize_current(target):
        if signature() != version_signature:
            raise ValueError('Destination code changed before initialization')
        initialize(target)

    def history(reader, target, cursor, limit, *, table):
        values, bookmark, done = source.scan(reader, table, cursor, limit)
        target.executemany('INSERT INTO '+quote(table)+' VALUES (?,?,?) '
            'ON CONFLICT(version) DO UPDATE SET name=excluded.name,applied_at=excluded.applied_at',
            ((row['version'], row['name'], row['applied_at']) for row in values))
        return Batch(bookmark, len(values), done)

    stages = tuple(Stage('copy:'+table,
        partial(history, table=table) if table in ('schema_migrations', 'schema_changes') else
        partial(_copy, table=table, mapping=mapping), batch_size=8192) for table, mapping in copies)

    def verify_current(reader, target):
        if signature() != version_signature:
            raise ValueError('Destination code changed during construction')
        current.validate(target)
        regular.validate(target)
        for table, mapping in copies:
            if table in ('schema_migrations', 'schema_changes'):
                for row in reader.execute('SELECT version,name,applied_at FROM '+quote(table)):
                    actual = target.execute('SELECT version,name,applied_at FROM '+quote(table)+' WHERE version=?', (row[0],)).fetchone()
                    if tuple(actual or ()) != tuple(row):
                        raise ValueError('Current rebuild changed applied history: '+table)
            else:
                verification._exact_sql(reader, target, table, table, mapping)

    return Destination(version_signature, initialize_current,
        (*stages, Stage('indexes', indexes), Stage('guards', finish)), verify_current)
