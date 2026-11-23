"""Keep metadata selection and retry state on its existing owners."""
import re

from .. import destination_schema, group_preview_work, metadata_runtime
from ..metadata_contract import with_genre_keys
from ..schema_contract import MigrationError, tokens
from .account_storage import _preserve_sequence, _sequence
from .track_source_cutover import _replace_table


NAME = 'fold_metadata_observation_storage'
GUARD_REPAIR = 'repair_metadata_selection_guards'
MARKER = 'ix_track_source_artwork_retry'
RETIRED = ('enrichment_artwork', 'metadata_runtime_recording_genres', 'metadata_runtime_genres',
           'metadata_runtime_providers', 'metadata_runtime_recording_sources',
           'metadata_runtime_entry_sources', 'entry_metadata_selections', 'metadata_runtime_conflicts')
FK_OWNERS = ('metadata_runtime_choices', 'recording_metadata_selections', 'metadata_conflicts', *RETIRED)
ARTWORK_COLUMNS = {
    'artwork_release_ids': 'TEXT',
    'artwork_status': 'TEXT',
    'artwork_failures': 'INTEGER',
    'artwork_retry_at': 'REAL',
    'artwork_error': 'TEXT',
}


def _artwork(conn):
    for column, declaration in ARTWORK_COLUMNS.items():
        conn.execute(f'ALTER TABLE track_sources ADD COLUMN {column} {declaration}')
    conn.execute('''UPDATE track_sources AS t SET artwork_release_ids=a.release_ids_json,
        artwork_status=a.status,artwork_failures=a.failures,artwork_retry_at=a.retry_at,artwork_error=a.error
        FROM enrichment_artwork a WHERE a.entry_id=t.id''')
    conn.execute(f'''CREATE INDEX {MARKER} ON track_sources(artwork_retry_at,id)
        WHERE artwork_status='retry' ''')
    conn.execute('DROP TABLE enrichment_artwork')


def _objects(conn, owners):
    reference = re.compile(r'\b(?:' + '|'.join(sorted(owners)) + r')\b')
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name")
        if row[2] in owners or reference.search(row[3])]


def predecessor_objects(conn):
    selected = {name for _, name, _, _ in _objects(conn, set(FK_OWNERS))}
    selected.update(('track_sources', 'enrichment_provider_config'))
    return [sql for name, sql in conn.execute(
        'SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name') if name in selected]


def installed(conn):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (MARKER,)).fetchone() is not None


def _providers(conn):
    conn.execute('ALTER TABLE enrichment_provider_config ADD COLUMN metadata_provider_id INTEGER '
                 'CHECK(metadata_provider_id IS NULL OR metadata_provider_id>0)')
    conn.execute('CREATE UNIQUE INDEX ix_metadata_provider_id ON enrichment_provider_config(metadata_provider_id)')
    conn.execute('''INSERT INTO enrichment_provider_config(provider,metadata_provider_id)
        SELECT name,id FROM metadata_runtime_providers WHERE true
        ON CONFLICT(provider) DO UPDATE SET metadata_provider_id=excluded.metadata_provider_id''')


def _choices(conn, sql):
    sql = (sql.replace('REFERENCES metadata_runtime_providers(id)',
                       'REFERENCES enrichment_provider_config(metadata_provider_id)')
           .replace('genre_id INTEGER REFERENCES metadata_runtime_genres(id)',
                    'genre_key TEXT REFERENCES genre_catalog(key)').replace('genre_id', 'genre_key'))
    highwater = _sequence(conn, 'metadata_runtime_choices')
    _replace_table(conn, 'metadata_runtime_choices', sql, expressions={
        'genre_key': '(SELECT genre_key FROM metadata_runtime_genres WHERE id=original.genre_id)'})
    _preserve_sequence(conn, 'metadata_runtime_choices', highwater)


def _selections(conn):
    def fields(names):
        return ','.join(f"'{name}'" for name in names)

    conn.execute(f'''CREATE TABLE _metadata_selections (
        recording_id INTEGER REFERENCES recordings(id),
        entry_id INTEGER REFERENCES track_sources(id),
        field TEXT NOT NULL,
        observation_id INTEGER REFERENCES metadata_observations(id),
        attribution_status TEXT CHECK(attribution_status IN ('evidenced','legacy_unresolved')),
        legacy_source TEXT,
        selected_at REAL CHECK(selected_at IS NULL OR (typeof(selected_at) IN ('integer','real')
            AND abs(selected_at)<=1.7976931348623157e308)),
        runtime_provider_id INTEGER REFERENCES enrichment_provider_config(metadata_provider_id),
        CHECK((recording_id IS NOT NULL AND entry_id IS NULL AND field IN ({fields((*metadata_runtime.RECORDING_FIELDS, 'energy'))}))
            OR (entry_id IS NOT NULL AND recording_id IS NULL AND field IN ({fields(metadata_runtime.ITEM_FIELDS)}))),
        CHECK((attribution_status IS NULL AND observation_id IS NULL AND legacy_source IS NULL AND selected_at IS NULL
                AND runtime_provider_id IS NOT NULL)
            OR (attribution_status IS NOT NULL AND ((attribution_status='evidenced'
                AND observation_id IS NOT NULL AND legacy_source IS NULL)
                OR (attribution_status='legacy_unresolved' AND observation_id IS NULL AND legacy_source IS NOT NULL)))),
        UNIQUE(recording_id,field),UNIQUE(entry_id,field))''')
    for scope in ('recording', 'entry'):
        table = f'{scope}_metadata_selections'
        identity = scope + '_id'
        columns = f'{identity},field,observation_id,attribution_status,legacy_source,selected_at'
        conn.execute(f'INSERT INTO _metadata_selections({columns}) SELECT {columns} FROM {table}')
        conn.execute(f'''INSERT INTO _metadata_selections({identity},field,runtime_provider_id)
            SELECT s.{identity},{metadata_runtime.field_expression('s')},s.provider_id
            FROM metadata_runtime_{scope}_sources s WHERE true
            ON CONFLICT({identity},field) DO UPDATE SET runtime_provider_id=excluded.runtime_provider_id''')
        conn.execute(f'DROP TABLE {table}')
    conn.execute('ALTER TABLE _metadata_selections RENAME TO recording_metadata_selections')
    for owner, subject in (('recordings', 'recording_id'), ('track_sources', 'entry_id')):
        conn.execute(f'''CREATE TRIGGER remove_{subject}_runtime_selection AFTER DELETE ON {owner}
            BEGIN
                DELETE FROM recording_metadata_selections WHERE {subject}=OLD.id AND attribution_status IS NULL;
                DELETE FROM metadata_conflicts WHERE {subject}=OLD.id AND runtime_id IS NOT NULL;
            END''')


def _conflicts(conn, sql):
    sql = (sql.replace('CHECK((entry_id IS NULL)', 'runtime_id INTEGER UNIQUE,runtime_revision INTEGER,'
            'CHECK((runtime_id IS NULL AND runtime_revision IS NULL) OR '
            '(runtime_id IS NOT NULL AND runtime_id>0 AND runtime_revision IS NOT NULL AND runtime_revision>0 '
            'AND selected_observation_id IS NULL AND legacy_record_id IS NULL)),'
            'CHECK((entry_id IS NULL)', 1)
        .replace("CHECK(status<>'user_resolved'", "CHECK(runtime_id IS NOT NULL OR status<>'user_resolved'", 1))
    highwater = max(_sequence(conn, 'metadata_conflicts') or 0, _sequence(conn, 'metadata_runtime_conflicts') or 0)
    _replace_table(conn, 'metadata_conflicts', sql, expressions={'runtime_id': 'NULL', 'runtime_revision': 'NULL'})
    _preserve_sequence(conn, 'metadata_conflicts', highwater)
    conn.execute(f'''INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,status,updated_at,
            runtime_id,runtime_revision)
        SELECT c.entry_id,c.recording_id,
            CASE WHEN c.recording_id IS NOT NULL THEN 'recording' WHEN c.field IN
                ({metadata_runtime.code('mb_year')},{metadata_runtime.code('label')}) THEN 'release' ELSE 'item' END,
            {metadata_runtime.field_expression('c')},
            CASE c.status WHEN 1 THEN 'user_resolved' WHEN 2 THEN 'provider_converged' ELSE 'unresolved' END,
            c.updated_at,c.id,c.revision FROM metadata_runtime_conflicts c''')
    for scope in ('entry', 'recording'):
        conn.execute(f'CREATE UNIQUE INDEX ix_runtime_conflict_{scope} ON metadata_conflicts({scope}_id,field) '
                     f'WHERE {scope}_id IS NOT NULL AND runtime_id IS NOT NULL')


def _definition(name, owner, sql):
    if owner in RETIRED and owner != 'entry_metadata_selections':
        return None
    if name in ('item_field_sources', 'recording_field_sources'):
        scope = 'entry' if name == 'item_field_sources' else 'recording'
        return metadata_runtime.selection_view(scope, runtime='metadata_runtime_providers' in sql)
    if name == 'enrichment_conflict':
        if 'FROM metadata_runtime_conflicts' in sql:
            return metadata_runtime.views(compact=True, include_choices=False, entry_owner='track_sources')[name]
        return sql + ' WHERE c.runtime_id IS NULL'
    if name in ('ix_metadata_conflict_entry', 'ix_metadata_conflict_recording'):
        return sql + ' AND runtime_id IS NULL'
    if name == 'immutable_metadata_conflict_subject':
        return sql.replace('legacy_record_id ON ', 'legacy_record_id,runtime_id ON ', 1)
    if owner in ('entry_metadata_selections', 'recording_metadata_selections') and name.startswith('owned_'):
        subject = 'entry_id' if owner == 'entry_metadata_selections' else 'recording_id'
        return (sql.replace(' ON entry_metadata_selections ', ' ON recording_metadata_selections ')
                .replace(' WHEN NEW.observation_id', f' WHEN NEW.{subject} IS NOT NULL AND NEW.observation_id'))
    if 'metadata_runtime_genres g' in sql:
        sql = with_genre_keys(sql)
    if owner == 'metadata_runtime_choices':
        sql = re.sub(r'\bgenre_id\b', 'genre_key', sql)
    return sql


def install(conn, _context):
    objects = _objects(conn, set(FK_OWNERS))
    for kind, name, _, _ in objects:
        if kind in ('trigger', 'view'):
            conn.execute(f'DROP {kind} "{name}"')
    _artwork(conn)
    _providers(conn)
    choice_definition = next(sql for _, name, _, sql in objects if name == 'metadata_runtime_choices')
    _choices(conn, choice_definition)
    _selections(conn)
    conflict_definition = next(sql for _, name, _, sql in objects if name == 'metadata_conflicts')
    _conflicts(conn, conflict_definition)
    for table in RETIRED:
        if table not in ('enrichment_artwork', 'entry_metadata_selections'):
            conn.execute(f'DROP TABLE {table}')
    for kind, name, owner, sql in objects:
        if kind == 'table' or kind == 'index' and owner in RETIRED:
            continue
        if kind == 'index' and owner not in FK_OWNERS:
            continue
        definition = _definition(name, owner, sql)
        if definition is not None:
            conn.execute(definition)
    group_preview_work.install_guards(conn, sources=group_preview_work.METADATA_SELECTION_SOURCES)
    conn.execute("UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0")


def validate(conn):
    if not installed(conn) or conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN (' +
            ','.join('?' for _ in RETIRED) + ') LIMIT 1', RETIRED).fetchone():
        raise MigrationError('Metadata observation storage was not folded')


def _selection_guards(conn):
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='recording_metadata_selections'").fetchone():
        return
    installed = dict(conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
                                  "AND tbl_name='recording_metadata_selections'"))
    statements = destination_schema.definition().get('metadata_selection_owner_predecessor_statements')
    for sql in statements if statements else destination_schema.statements():
        if not sql.startswith(('CREATE TRIGGER owned_entry_metadata_selections_',
                               'CREATE TRIGGER owned_recording_metadata_selections_')):
            continue
        name = sql.split()[2]
        subject = 'entry_id' if name.startswith('owned_entry_') else 'recording_id'
        previous = sql.replace(f'NEW.{subject} IS NOT NULL AND ', '', 1)
        yield name, installed.get(name), previous, sql


def previous_selection_guards(conn):
    """Recognize the exact trigger definitions installed by the first C121 implementation."""
    return {name: ('trigger', tokens(previous)) for name, installed, previous, _ in _selection_guards(conn)
            if tokens(installed) == tokens(previous)}


def repair_selection_guards(conn, _context):
    for name, installed, previous, sql in _selection_guards(conn):
        if tokens(installed) == tokens(previous):
            conn.execute(f'DROP TRIGGER {name}')
            conn.execute(sql)


def validate_selection_guards(conn):
    for name, installed, _, sql in _selection_guards(conn):
        if tokens(installed) != tokens(sql):
            raise MigrationError(f'Incompatible current trigger: {name}')
