"""Construct and verify database candidates without deleting from their source.

construct() runs the schema owner's direct conversions with atomic bookmarks.
build() retains the existing installed-format compaction contract until the
normalized destination and its source mappings are integrated. Neither installs
a database or advances source migration history.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable

from .. import db_handles, db_space
from ..migrations import regular
from ..migrations.registry import MIGRATIONS
from ..migrations.runner import history, validate_version
from ..policy_recovery import _verify


@dataclass(frozen=True)
class Batch:
    """A domain conversion's committed bookmark and number of copied records."""
    cursor: object = None
    rows: int = 0
    done: bool = False


@dataclass(frozen=True)
class Stage:
    name: str
    copy: Callable


@dataclass(frozen=True)
class Destination:
    """The schema owner's versioned contract for a direct rebuild.

    initialize(target) creates the destination once. Each stage's
    copy(source, target, cursor, limit) returns a Batch without committing.
    verify(source, target) establishes the domain preservation requirements.
    Source admission and domain mappings remain explicit at the caller.
    """
    version: str
    initialize: Callable
    stages: tuple[Stage, ...]
    verify: Callable


CHECKPOINT = '_database_rebuild_checkpoint'


def _source_identity(path):
    """Conservative cross-process identity; any source write invalidates reuse.

    The caller holds write admission. Include WAL bytes rather than treating
    SQLite's connection-local data_version as a persistent change counter.
    """
    result = {}
    for candidate in (path, path.with_name(path.name+'-wal')):
        try:
            with candidate.open('rb') as stream:
                # A read-only WAL connection may create an empty sidecar. That
                # contains no committed source data and is not a source change.
                if candidate != path and candidate.stat().st_size == 0:
                    continue
                result[candidate.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        except FileNotFoundError:
            if candidate == path:
                raise
    return {'path': str(path), 'files': result}


def _checkpoint(conn, value=None):
    if value is not None:
        document = json.dumps(value, sort_keys=True, allow_nan=False)
        conn.execute('UPDATE '+CHECKPOINT+' SET document=? WHERE singleton=1', (document,))
    row = conn.execute('SELECT document FROM '+CHECKPOINT+' WHERE singleton=1').fetchone()
    if row is None:
        raise ValueError('Candidate has no rebuild checkpoint')
    return json.loads(row[0])


def _owned_transaction(conn):
    """Domain callbacks must not publish a batch outside its bookmark commit."""
    def authorize(action, *_):
        return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT) else sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)


def construct(source, output, destination, *, batch_size=128, progress=lambda phase, detail: None,
              cancelled=lambda: False):
    """Build or resume a fresh-schema candidate with atomic batch checkpoints.

    Only the destination is written. A failed batch rolls back with its cursor;
    completed batches remain resumable while the exact source and destination
    contract are unchanged. A candidate is publishable only after this function
    returns: final validation precedes removal of its private checkpoint table.
    The job's immutable verified recovery copy is the stable restart source;
    checkpointing an active serving WAL conservatively invalidates its identity.
    """
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError('Rebuild batch size must be a positive integer')
    names = [stage.name for stage in destination.stages]
    if (not isinstance(destination.version, str) or not destination.version
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('Rebuild needs a versioned destination and unique named stages')
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    if output.is_symlink() or output.resolve() == source:
        raise ValueError('Rebuild output must be a separate owned candidate')
    started = time.monotonic()
    with db_handles.lease(source), closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as reader:
        _configure(reader, cancelled)
        observed_version = reader.execute('PRAGMA data_version').fetchone()[0]
        reader.execute('BEGIN')
        reader.execute('SELECT name FROM main.sqlite_schema LIMIT 1').fetchone()
        identity = _source_identity(source)
        if not output.exists():
            used = (reader.execute('PRAGMA page_count').fetchone()[0]
                    - reader.execute('PRAGMA freelist_count').fetchone()[0])
            db_space.require_space(output.parent, used*reader.execute('PRAGMA page_size').fetchone()[0]+4*1024**2)
            with output.open('xb'):
                pass
            output.chmod(0o600)
            try:
                with closing(sqlite3.connect(output)) as target:
                    _configure(target, cancelled)
                    target.execute('PRAGMA journal_mode=DELETE')
                    target.execute('PRAGMA synchronous=FULL')
                    target.execute('BEGIN IMMEDIATE')
                    _owned_transaction(target)
                    try:
                        destination.initialize(target)
                    finally:
                        target.set_authorizer(None)
                    target.execute('CREATE TABLE '+CHECKPOINT+'('
                        'singleton INTEGER PRIMARY KEY CHECK(singleton=1),document TEXT NOT NULL)')
                    initial = dict(format=1, source=identity, destination=destination.version,
                                   stages=names, stage=0, cursor=None, rows={})
                    target.execute('INSERT INTO '+CHECKPOINT+' VALUES(1,?)',
                                   (json.dumps(initial, sort_keys=True, allow_nan=False),))
                    target.commit()
            except BaseException:
                output.unlink(missing_ok=True)
                raise
        with closing(sqlite3.connect(output)) as target:
            _configure(target, cancelled)
            target.execute('PRAGMA synchronous=FULL')
            try:
                pending = _checkpoint(target)
                if (pending.get('format') != 1 or pending.get('source') != identity
                        or pending.get('destination') != destination.version or pending.get('stages') != names):
                    raise ValueError('Rebuild source or destination contract changed; discard the stale candidate')
                while pending['stage'] < len(destination.stages):
                    if cancelled():
                        raise InterruptedError('Database rebuild interrupted')
                    stage = destination.stages[pending['stage']]
                    target.execute('BEGIN IMMEDIATE')
                    _owned_transaction(target)
                    try:
                        batch = stage.copy(reader, target, pending['cursor'], batch_size)
                    finally:
                        target.set_authorizer(None)
                    if (not isinstance(batch, Batch) or not isinstance(batch.rows, int)
                            or isinstance(batch.rows, bool) or batch.rows < 0
                            or not isinstance(batch.done, bool)
                            or not batch.done and batch.cursor == pending['cursor']):
                        raise ValueError('Conversion stage did not provide a valid advancing batch: '+stage.name)
                    # Serialization is part of the same transaction as the rows.
                    pending['rows'][stage.name] = pending['rows'].get(stage.name, 0)+batch.rows
                    pending['cursor'] = None if batch.done else batch.cursor
                    pending['stage'] += int(batch.done)
                    _checkpoint(target, pending)
                    target.commit()
                    progress('copy', {'stage': stage.name, 'rows': pending['rows'][stage.name],
                                      'cursor': pending['cursor']})
                if cancelled():
                    raise InterruptedError('Database rebuild interrupted')
                progress('verify', {'destination': destination.version})
                target.execute('PRAGMA query_only=ON')
                target.execute('BEGIN')
                _owned_transaction(target)
                try:
                    destination.verify(reader, target)
                    _verify(target)
                finally:
                    target.set_authorizer(None)
                    target.rollback()
                    target.execute('PRAGMA query_only=OFF')
                # Inspect data_version outside the pinned snapshot as well as
                # the persistent fingerprint. A commit between pinning that
                # snapshot and hashing could otherwise make both hashes agree
                # on newer bytes while the conversion read older data.
                reader.rollback()
                if (reader.execute('PRAGMA data_version').fetchone()[0] != observed_version
                        or _source_identity(source) != identity):
                    raise ValueError('Source changed during database rebuild')
                target.execute('BEGIN IMMEDIATE')
                target.execute('DROP TABLE '+CHECKPOINT)
                target.commit()
                report = dict(source=str(source), output=str(output), destination=destination.version,
                              rows=pending['rows'], sql_bytes=output.stat().st_size,
                              seconds=time.monotonic()-started)
            except BaseException:
                target.set_authorizer(None)
                target.rollback()
                raise
    return report


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def _columns(conn, name, schema='main'):
    return list(conn.execute('PRAGMA ' + schema + '.table_xinfo(' + quote(name) + ')'))


def _fields(conn, name):
    columns = _columns(conn, name)
    names = [column[1] for column in columns if column[6] == 0]
    definition = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()[0]
    if 'WITHOUT ROWID' not in definition.upper():
        # Preserve implicit IDs even on tables with a separate composite PK.
        hidden = next((alias for alias in ('rowid', '_rowid_', 'oid')
                       if alias not in {field.lower() for field in names}), None)
        if hidden is None:
            raise ValueError('Compaction cannot preserve hidden row IDs for ' + name)
        return [hidden, *names], [hidden]
    keys = [column[1] for column in sorted(columns, key=lambda row: row[5]) if column[5]]
    return names, keys


def _references(conn, table, parent):
    """Concrete FK references, including future owners unknown to cleanup code."""
    groups = {}
    for row in conn.execute('PRAGMA main.foreign_key_list(' + quote(table) + ')'):
        if row[2] == parent:
            groups.setdefault(row[0], []).append(row)
    return groups.values()


def plan(conn, *, now):
    """Return table predicates; omission requires a positive domain rule.

    Source tables stay read-only in ``main``. Scratch sets are TEMP tables on
    this private connection, never tables in the serving database.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
    predicates = {}
    generations = {'recommendation_generations', 'recommendation_heads', 'recommendation_generation_refs',
                   'recommendation_builds'}
    if not generations <= tables:
        return predicates
    owned = ('prepared_outputs', 'recommendation_bundle_framing', 'recommendation_surface_pools',
             'recommendation_build_pools', 'recommendation_generation_surfaces', 'recommendation_builds')
    conn.execute('CREATE TEMP TABLE compact_drop_generations(id INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO compact_drop_generations SELECT g.id FROM main.recommendation_generations g '
        'WHERE NOT EXISTS(SELECT 1 FROM main.recommendation_heads h WHERE h.generation_id=g.id) '
        'AND NOT EXISTS(SELECT 1 FROM main.recommendation_generation_refs r WHERE r.generation_id=g.id) '
        "AND (g.status IN ('published','retiring') OR EXISTS(SELECT 1 FROM main.recommendation_builds b "
        'WHERE b.generation_id=g.id AND b.expires_at<?))', (now,))
    # Domain cleanup owns these children. An additional FK owner is evidence,
    # even if it was introduced after this compactor's original implementation.
    for table in sorted(tables - set(owned)):
        for reference in _references(conn, table, 'recommendation_generations'):
            if len(reference) != 1 or reference[0][4] not in ('id', None):
                raise ValueError('Unsupported recommendation generation reference: ' + table)
            column = quote(reference[0][3])
            conn.execute('DELETE FROM compact_drop_generations WHERE EXISTS(SELECT 1 FROM main.'
                         + quote(table) + ' r WHERE r.' + column + '=compact_drop_generations.id)')
    predicates['recommendation_generations'] = 's.id NOT IN (SELECT id FROM compact_drop_generations)'
    for table in owned:
        if table in tables:
            predicates[table] = 's.generation_id NOT IN (SELECT id FROM compact_drop_generations)'
    if 'recommendation_pools' not in tables:
        return predicates
    owners = ('recommendation_surface_pools', 'recommendation_build_pools', 'prepared_outputs')
    if not set(owners) <= tables:
        return predicates
    conn.execute('CREATE TEMP TABLE compact_drop_pools(id INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO compact_drop_pools SELECT p.id FROM main.recommendation_pools p WHERE '
        + ' AND '.join('NOT EXISTS(SELECT 1 FROM main.' + table + ' s WHERE s.pool_id=p.id AND ('
                       + predicates.get(table, '1') + '))' for table in owners))
    children = ('recommendation_radio_bands', 'recommendation_candidates',
                'unresolved_recommendation_candidates', 'recommendation_pool_retirements')
    for table in sorted(tables - set(owners) - set(children)):
        for reference in _references(conn, table, 'recommendation_pools'):
            if len(reference) != 1 or reference[0][4] not in ('id', None):
                raise ValueError('Unsupported recommendation pool reference: ' + table)
            conn.execute('DELETE FROM compact_drop_pools WHERE EXISTS(SELECT 1 FROM main.'
                + quote(table) + ' r WHERE r.' + quote(reference[0][3]) + '=compact_drop_pools.id)')
    predicates['recommendation_pools'] = 's.id NOT IN (SELECT id FROM compact_drop_pools)'
    for table in children:
        if table in tables:
            predicates[table] = 's.pool_id NOT IN (SELECT id FROM compact_drop_pools)'
    return predicates


def _equal_rows(conn, name, predicate):
    fields, keys = _fields(conn, name)
    expected = conn.execute('SELECT count(*) FROM main.' + quote(name) + ' s WHERE ' + predicate).fetchone()[0]
    actual = conn.execute('SELECT count(*) FROM compacted.' + quote(name)).fetchone()[0]
    if expected != actual:
        raise ValueError('Compaction changed retained row count: ' + name)
    # Each key is unique, including implicit row IDs. Count equality plus every
    # expected key/value match establishes a bijection without huge EXCEPT sorts.
    generated = [column[1] for column in _columns(conn,name) if column[6] in (2,3)]
    # Use the installed key comparison for indexed lookup, then compare every
    # value independently of affinity/collation. SQL equality alone admits
    # changed NOCASE text and equates INTEGER 1 with REAL 1.0.
    comparison = ' AND '.join('d.' + quote(key) + ' IS s.' + quote(key) for key in keys)
    for field in dict.fromkeys([*keys, *fields, *generated]):
        left, right = 'd.' + quote(field), 's.' + quote(field)
        comparison += f' AND typeof({left})=typeof({right}) AND {left} COLLATE BINARY IS {right} COLLATE BINARY'
    missing = conn.execute('SELECT 1 FROM main.' + quote(name) + ' s WHERE (' + predicate
        + ') AND NOT EXISTS(SELECT 1 FROM compacted.' + quote(name) + ' d WHERE ' + comparison + ') LIMIT 1').fetchone()
    if missing:
        raise ValueError('Compaction changed retained values or IDs: ' + name)
    return actual


def _format(conn):
    version = history(conn, MIGRATIONS)
    if version < 79:
        raise ValueError('Retained-copy compaction requires a supported installed format (79 or later)')
    validate_version(conn, version, foreign_keys=False, data=False)
    regular.validate(conn)
    return version


def _configure(conn, cancelled=lambda:False):
    from yt_playlist.util.matching import search_squash
    from yt_playlist.util.genre_catalog import normalize
    conn.create_function('searchnorm',1,search_squash,deterministic=True)
    conn.create_function('tastenorm',1,normalize,deterministic=True)
    conn.set_progress_handler(lambda:int(cancelled()),1000)


def build(source, output, *, now=None, progress=lambda phase, detail: None, cancelled=lambda: False):
    """Produce an independently checked candidate; caller owns write admission.

    Concurrent commits reject the candidate. This is an additional stale-copy
    check, not a replacement for the application's maintenance/handle fence.
    Required file custody is established separately before installation.
    """
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    if output.exists() or output.is_symlink() or source == output:
        raise ValueError('Compaction output must be a new database')
    started = time.monotonic()
    report = {'source': str(source), 'output': str(output), 'tables': {}, 'phases': {}}
    with db_handles.lease(source), closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as observed:
        version = observed.execute('PRAGMA data_version').fetchone()[0]
        report['installed_version'] = _format(observed)
        page_size = observed.execute('PRAGMA page_size').fetchone()[0]
        used = (observed.execute('PRAGMA page_count').fetchone()[0]
                - observed.execute('PRAGMA freelist_count').fetchone()[0]) * page_size
        # This conservative upper bound includes all original indexes. No
        # deletion, staging backup or historical conversion precedes the copy.
        db_space.require_space(output.parent, used + 4 * 1024**2)
        with output.open('xb'):
            pass
        output.chmod(0o600)
        try:
            schema = list(observed.execute('SELECT type,name,sql FROM main.sqlite_schema '
                "WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY name"))
            tables = [name for kind,name,_ in schema if kind=='table']
            phase = time.monotonic()
            with closing(sqlite3.connect(output.as_uri(), uri=True)) as target:
                _configure(target,cancelled)
                target.execute(f'PRAGMA page_size={page_size}')
                target.execute('PRAGMA journal_mode=DELETE')
                target.execute('PRAGMA synchronous=FULL')
                target.execute('BEGIN IMMEDIATE')
                for kind, name, sql in schema:
                    if kind == 'table':
                        if 'VIRTUAL TABLE' in sql.upper():
                            raise ValueError('Unsupported virtual compaction owner: ' + name)
                        target.execute(sql)
                target.commit()
            # Installed preparation guards may explicitly qualify their source
            # as main. Keep that database in main and attach only the guard-free
            # new tables; never reinterpret an installed trigger under an alias.
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as copier:
                _configure(copier,cancelled)
                copier.execute('ATTACH DATABASE ? AS compacted',(output.as_uri(),))
                copier.execute('BEGIN')
                predicates = plan(copier, now=time.time() if now is None else now)
                report['phases']['schema_and_plan_seconds'] = time.monotonic() - phase
                phase = time.monotonic()
                for name in tables:
                    fields, _ = _fields(copier, name)
                    columns = ','.join(map(quote, fields))
                    predicate = predicates.get(name, '1')
                    began = time.monotonic()
                    count = copier.execute('INSERT INTO compacted.' + quote(name) + '(' + columns + ') SELECT '
                        + columns + ' FROM main.' + quote(name) + ' s WHERE ' + predicate).rowcount
                    report['tables'][name] = {'rows': count, 'copy_seconds': time.monotonic() - began}
                    progress('copy', {'table': name, 'rows': count})
                if copier.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
                    copier.execute('DELETE FROM compacted.sqlite_sequence')
                    copier.execute('INSERT INTO compacted.sqlite_sequence SELECT * FROM main.sqlite_sequence')
                for pragma in ('user_version', 'application_id'):
                    value = copier.execute('PRAGMA main.' + pragma).fetchone()[0]
                    copier.execute('PRAGMA compacted.' + pragma + '=' + str(value))
                copier.commit()
                report['phases']['copy_seconds'] = time.monotonic() - phase
                phase = time.monotonic()
                copier.execute('BEGIN')
                for name in tables:
                    _equal_rows(copier,name,predicates.get(name,'1'))
                    progress('verify',{'table':name})
                if copier.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
                    if copier.execute('SELECT * FROM main.sqlite_sequence ORDER BY name').fetchall() != \
                            copier.execute('SELECT * FROM compacted.sqlite_sequence ORDER BY name').fetchall():
                        raise ValueError('Compaction changed sequence high-water marks')
                copier.rollback()
                copier.execute('DETACH DATABASE compacted')
                report['phases']['row_validation_seconds'] = time.monotonic()-phase
            phase = time.monotonic()
            with closing(sqlite3.connect(output.as_uri(),uri=True)) as target:
                _configure(target,cancelled)
                target.execute('BEGIN IMMEDIATE')
                for kind in ('index', 'view', 'trigger'):
                    for actual, name, sql in schema:
                        if actual == kind:
                            target.execute(sql)
                            progress(kind, {'name': name})
                target.commit()
                target.execute('PRAGMA foreign_keys=ON')
                report['phases']['indexes_guards_and_commit_seconds'] = time.monotonic() - phase
                phase = time.monotonic()
                target.execute('BEGIN')
                actual_schema = {tuple(row) for row in target.execute('SELECT type,name,sql FROM main.sqlite_schema '
                    "WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*'")}
                if actual_schema != {tuple(row) for row in schema}:
                    raise ValueError('Compaction changed installed schema definitions')
                _verify(target)
                if _format(target) != report['installed_version']:
                    raise ValueError('Compaction changed installed migration history')
                report['phases']['validation_seconds'] = time.monotonic() - phase
                report['freelist_pages'] = target.execute('PRAGMA freelist_count').fetchone()[0]
                report['sql_bytes'] = output.stat().st_size
                report['index_bytes'] = target.execute("SELECT coalesce(sum(pgsize),0) FROM dbstat "
                    "WHERE name IN (SELECT name FROM sqlite_schema WHERE type='index')").fetchone()[0]
                target.rollback()
            if observed.execute('PRAGMA data_version').fetchone()[0] != version:
                raise ValueError('Source changed during retained-copy compaction')
        except BaseException:
            # A failed build is never installable. Recovery state lives outside
            # this candidate, and the source has only been opened read-only.
            output.unlink(missing_ok=True)
            raise
    report['seconds'] = time.monotonic() - started
    return report
