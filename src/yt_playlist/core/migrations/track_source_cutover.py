"""Fold the source tables and their foreign keys in the installed database."""
import re

from .. import (catalog_metadata, file_receipts, group_preview_work, library_scope_changes,
                metadata_runtime, prepared_input_schema, recording_metadata, source_metadata,
                track_sources, vibe_versions)
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from . import track_source_preparation as preparation
from .control_settings_storage import predecessor_metadata_active


RETIRED = {'catalog_entries', 'youtube_items', 'unresolved_items', 'library_memberships',
           'catalog_metadata_state', 'recording_metadata_state'}
SOURCE_FIELDS = ('id', 'recording_id', 'video_id', 'source_ref', *track_sources.RAW_COLUMNS,
                 'library_origin', 'library_added_at')


def object_name(sql):
    return re.match(r'CREATE (?:UNIQUE )?(\w+)\s+"?([\w]+)', sql, re.I)[2]


def _ownership_sql(kind, sql):
    owner = re.search(r'\bON\s+"?(youtube_items|unresolved_items)\b', sql, re.I)
    if owner is None:
        return sql
    upload = owner[1] == 'youtube_items'
    sql = sql[:owner.start(1)] + 'track_sources' + sql[owner.end(1):]
    if not upload:
        sql = re.sub(r'\b(OLD|NEW)\.entry_id\b', r'\1.id', sql)
    if kind == 'index':
        return sql if upload else re.sub(r'\bentry_id\b', 'id', sql)
    heading, body = re.split(r'\s+BEGIN\s+', sql, maxsplit=1, flags=re.I)
    identity = 'OLD' if re.search(r'\bDELETE\s+ON\b', heading, re.I) else 'NEW'
    predicate = identity + ('.video_id IS NOT NULL' if upload else '.video_id IS NULL')
    head, *condition = re.split(r'\s+WHEN\s+', heading, maxsplit=1, flags=re.I)
    when = f'{predicate} AND ({condition[0]})' if condition else predicate
    return f'{head} WHEN {when} BEGIN {body}'


def folded_definition(kind, name, sql):
    if kind == 'table':
        sql = track_sources.folded_foreign_keys(sql)
        if name == 'recordings':
            for column in recording_metadata.SELECTION_COLUMNS.keys() - recording_metadata.CANONICAL_COLUMNS.keys():
                sql = re.sub(rf',\s*{column}\s+(?:TEXT|INTEGER)\b', '', sql)
        return sql
    if kind in ('index', 'trigger'):
        sql = sql.replace(name, name.replace('catalog_entries', 'track_sources'), 1)
        sql = _ownership_sql(kind, sql)
    return track_sources.folded_sql(sql)


def retired_object(name):
    return name in _RETIRED_OBJECTS or name.startswith((
        'group_preview_changed_catalog_entries_', 'group_preview_changed_youtube_items_',
        'group_preview_changed_unresolved_items_', 'group_preview_changed_library_memberships_',
        'rec_capture_library_memberships_', 'rec_content_library_memberships_'))


_RETIRED_OBJECTS = RETIRED | {'track_sources', 'ix_catalog_metadata_pending', 'entry_field_choices'} | {
    name for name, _ in (*recording_metadata.triggers(), *catalog_metadata._triggers(),
                        *library_scope_changes.triggers()) if not name.startswith('catalog_metadata_audit_')
}


def source_definitions():
    yield from track_sources.projections()
    yield from source_metadata.guards()
    yield from (sql for _, sql in recording_metadata.triggers(folded=True))
    yield from (sql for _, sql in library_scope_changes.triggers(folded=True))


def retire_energy(name, sql):
    if name == 'recordings':
        return re.sub(r',\s*energy REAL\b', '', sql)
    if name in ('prepared_recording_inputs', 'unresolved_preparation_inputs'):
        for scope in ('library', 'discovered'):
            column = scope + '_energy'
            constraint = (f',{column} REAL CHECK({column} IS NULL OR '
                          f"(typeof({column}) IN ('real','integer') AND abs({column})<=1.7976931348623157e308))")
            sql = sql.replace(constraint, '').replace(f' AND {column} IS NULL', '')
            sql = sql.replace(f',CHECK(({scope}_audio_fields & 2)<>0 OR {column} IS NULL)', '')
        return sql
    if name in ('rec_capture_recordings_update', 'rec_content_recordings_update',
                'group_preview_changed_recordings_update'):
        return sql.replace(' OR OLD.energy IS NOT NEW.energy', '').replace(' OR OLD."energy" IS NOT NEW."energy"', '')
    return _PREPARED_WRITES.get(name, sql)


_PREPARED_WRITES = dict(prepared_input_schema.definitions())


def _columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table}")') if row[6] == 0]


def _copy_table(conn, name, sql, version=None, *, expressions=None):
    staging = '_migration_' + name
    conn.execute(re.sub(r'^CREATE TABLE "?\w+"?', f'CREATE TABLE "{staging}"', sql, count=1, flags=re.I))
    columns = [f'"{column}"' for column in _columns(conn, staging)]
    expressions = expressions or {}
    projection = [expressions.get(column.strip('"'), column) for column in columns]
    if version is not None:
        conn.execute(f'INSERT INTO vibe_versions(value) SELECT DISTINCT "{version}" FROM "{name}" '
                     'WHERE true ON CONFLICT(value) DO NOTHING')
        projection[columns.index(f'"{version}_id"')] = (
            f'(SELECT id FROM vibe_versions WHERE value=original."{version}")')
    conn.execute(f'INSERT INTO "{staging}"({",".join(columns)}) '
                 f'SELECT {",".join(projection)} FROM "{name}" AS original')
    return staging


def _replace_table(conn, name, sql, version=None, *, expressions=None):
    # SQLite's normal DROP/RENAME procedure keeps the other owners' foreign
    # keys pointed at the canonical name. Foreign keys are checked before commit.
    staging = _copy_table(conn, name, sql, version, expressions=expressions)
    conn.execute(f'DROP TABLE "{name}"')
    conn.execute(f'ALTER TABLE "{staging}" RENAME TO "{name}"')


def _plan(conn):
    versions = {} if vibe_versions.normalized(conn) else vibe_versions.COLUMNS
    vibe_relations = dict(vibe_versions.application_definitions(conn)) if versions else {}
    objects = conn.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                           "WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' AND name NOT GLOB ?",
                           (preparation.PREFIX + '*',)).fetchall()
    copies = [preparation.Copy('track_sources', track_sources.TABLE)]
    drops, indexes, views, guards = [], [], [], []
    for kind, name, owner, sql in objects:
        if name in vibe_relations:
            continue
        if retired_object(name):
            if kind != 'table':
                drops.append((kind, name, owner))
            continue
        replacement = folded_definition(kind, name, retire_energy(name, sql))
        if kind == 'table':
            if name in versions:
                replacement = vibe_versions.transform(replacement, name)
            if name == 'audio_annotations':
                replacement = file_receipts.native_annotation_definition(replacement)
            if replacement != sql:
                copies.append(preparation.Copy(name, replacement, versions.get(name)))
        else:
            destination = {'index': indexes, 'view': views, 'trigger': guards}[kind]
            destination.append((name, sql, replacement, owner))
    return copies, drops, indexes, views, guards, bool(versions)


def prepare(conn, _version):
    if track_sources.pending(conn):
        preparation.prepare(conn, _plan(conn)[0])


def _install_stage(conn, stage, name, sql):
    current = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (stage,)).fetchone()
    if current is None or tokens(current[0]) != tokens(preparation.stage_definition(stage, sql)):
        raise MigrationError('Incompatible track source preparation table: ' + stage)
    conn.execute(f'ALTER TABLE "{stage}" RENAME TO "{name}"')


def _restore_foreign_keys(conn, tables):
    # The caller must preserve the layout of stored rows and existing indexes.
    # Reuse their b-trees and let the migration runner audit foreign keys
    # before committing the schema and history together.
    version = conn.execute('PRAGMA main.schema_version').fetchone()[0]
    conn.execute('PRAGMA writable_schema=ON')
    try:
        conn.executemany("UPDATE main.sqlite_schema SET sql=? WHERE type='table' AND name=?", tables)
        conn.execute(f'PRAGMA main.schema_version={version + 1}')
    finally:
        conn.execute('PRAGMA writable_schema=RESET')


def install(conn, _context):
    if not track_sources.pending(conn):
        return
    copies, drops, indexes, views, guards, versions = _plan(conn)
    preparation.require_complete(conn, copies)
    preparation.require_matching_sources(conn)
    if versions:
        vibe_versions.drop_application_relations(conn)
    highwaters = dict(conn.execute('SELECT name,seq FROM sqlite_sequence'))

    changed = ({copy.name for copy in copies} - {'track_sources'}) | {'recording_additional_artists'}
    removed = changed | RETIRED
    for kind, name, owner in drops:
        if kind == 'view' or owner not in removed:
            conn.execute(f'DROP {kind} "{name}"')
    # Dropping an old table also drops its indexes and triggers. Relations on
    # surviving owners need a separate DROP before their replacements install.
    restore = {}
    for kind, objects in (('view', views), ('index', indexes), ('trigger', guards)):
        restore[kind] = []
        for name, sql, replacement, owner in objects:
            if replacement != sql or owner in removed:
                if kind == 'view' or owner not in removed:
                    conn.execute(f'DROP {kind} "{name}"')
                restore[kind].append(replacement)

    for copy in copies:
        if copy.name in changed:
            conn.execute(f'DROP TABLE "{copy.name}"')
        _install_stage(conn, copy.stage, copy.name, copy.sql)
    conn.execute('DROP TABLE recording_additional_artists')
    _install_stage(conn, preparation.CREDITS, 'recording_additional_artists', recording_metadata.CREDITS)
    for table in RETIRED:
        conn.execute(f'DROP TABLE "{table}"')
    preparation.clear(conn)
    _restore_foreign_keys(conn, [(copy.sql, copy.name) for copy in copies] +
                          [(recording_metadata.CREDITS, 'recording_additional_artists')])
    highwaters['track_sources'] = highwaters.get('catalog_entries', 0)
    for name in changed | {'track_sources'}:
        if name in highwaters:
            conn.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (highwaters[name], name))
            conn.execute('INSERT INTO sqlite_sequence(name,seq) SELECT ?,? '
                         'WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=?)',
                         (name, highwaters[name], name))

    for sql in restore['index']:
        conn.execute(sql)
    for sql in (*restore['view'], *restore['trigger'], *source_definitions()):
        if object_name(sql) != 'catalog_tracks':
            conn.execute(sql)
    conn.execute('DROP VIEW IF EXISTS catalog_tracks')
    conn.execute(catalog_metadata.track_view(conn, canonical=True))
    if versions:
        vibe_versions.install_application_relations(conn)
    if predecessor_metadata_active(conn):
        metadata_runtime.replace_views(conn, entry_owner='track_sources',
                                       include_choices=not catalog_metadata.ready(conn))
    group_preview_work.install_guards(conn, sources={'track_sources'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate(conn):
    if not track_sources.folded(conn):
        raise MigrationError('Track source migration did not install the source table')
    if conn.execute('SELECT 1 FROM sqlite_schema WHERE name GLOB ? LIMIT 1', (preparation.PREFIX + '*',)).fetchone():
        raise MigrationError('Track source preparation storage remains after cutover')
    track_sources.validate(conn)


def retire_recording_metadata_progress(conn, _context):
    # The first source fold left its completed progress table behind.
    state = definitions(conn, ('recording_metadata_state',)).get('recording_metadata_state')
    if state is None:
        return
    if tokens(state) != tokens(recording_metadata.STATE) or not conn.execute(
            'SELECT 1 FROM recording_metadata_state WHERE id=1 AND ready=1').fetchone():
        raise MigrationError('Incompatible recording metadata progress after the track source fold')
    conn.execute('DROP TABLE recording_metadata_state')


def validate_retired_recording_metadata_progress(conn):
    if recording_metadata.installed(conn):
        raise MigrationError('Retired recording metadata progress table remains')
