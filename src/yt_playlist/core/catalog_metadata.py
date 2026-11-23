"""Installed catalog metadata layouts and the retained decision audit."""
from . import metadata_history, metadata_values, track_sources
from .metadata_values import RUNTIME_CODES as CODES
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions


FIELDS = (*metadata_values.ITEM_FIELDS, *metadata_values.RECORDING_FIELDS)
COLUMNS = {f'effective_{field}': metadata_values.column_type(field) for field in FIELDS if field != 'genre'}
COLUMNS.update(effective_genre_key='TEXT', effective_unresolved_genre='TEXT')
STATE = 'CREATE TABLE catalog_metadata_state (id INTEGER PRIMARY KEY CHECK(id=1), ready INTEGER NOT NULL CHECK(ready IN (0,1)))'
AUDIT = '''CREATE TABLE catalog_metadata_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL, recording_id INTEGER NOT NULL,
    field TEXT NOT NULL, source TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('select','edit','resolve','reset')),
    value, genre_key TEXT, changed_at REAL NOT NULL)'''
PENDING = 'CREATE INDEX ix_catalog_metadata_pending ON catalog_entries(effective_ready) WHERE effective_ready=0'


def installed(conn):
    return bool(definitions(conn, ('catalog_metadata_audit',)))


def ready(conn):
    if track_sources.folded(conn):
        return True
    row = conn.execute('SELECT ready FROM catalog_metadata_state WHERE id=1').fetchone()
    if row is None:
        raise MigrationError('Missing catalog metadata state')
    return bool(row[0])


def _has_energy(conn):
    return any(row[1] == 'energy' for row in conn.execute('PRAGMA main.table_info(recordings)'))


def track_view(conn, *, canonical, inline_genre_redirects=False):
    columns = ['e.id', 'e.recording_id', 'e.video_id', 'r.identity_key']
    has_energy = _has_energy(conn)
    genres = 'r' if canonical else 'e'
    source = 'track_sources' if track_sources.folded(conn) else 'catalog_entries'
    for field in FIELDS:
        owner = 'r' if canonical and field in metadata_values.CANONICAL_FIELDS else 'e'
        if field == 'genre':
            columns.append(f'COALESCE(g.name,{genres}.effective_unresolved_genre) AS genre')
        else:
            columns.append(f'{owner}.effective_{field} AS {field}')
        if field == 'bpm' and has_energy:
            columns.append('r.energy AS energy')
    columns.extend(('r.created_at', 'r.first_enriched_at', 'r.last_enriched_at'))
    genre_key = f'{genres}.effective_genre_key'
    if inline_genre_redirects:
        redirect_join = f'LEFT JOIN genre_catalog d ON d.key={genre_key} '
        target = 'd.redirect_key'
    else:
        redirect_join = f'LEFT JOIN genre_redirects d ON d.retired_key={genre_key} '
        target = 'd.genre_key'
    return ('CREATE VIEW catalog_tracks AS SELECT ' + ','.join(columns) +
        f' FROM {source} e JOIN recordings r ON r.id=e.recording_id '
        f'{redirect_join}LEFT JOIN genre_catalog g ON g.key=COALESCE({target},{genre_key})')


def _refresh_values(*, source=False):
    # Resolve choices on writes. Each choice contributes a presence bit, so a
    # selected null or blank survives updates to its underlying provider value.
    choices = '''WITH choices AS (
        SELECT c.field, COALESCE(c.number_value,c.text_value) AS value, g.genre_key
        FROM metadata_runtime_choices c LEFT JOIN metadata_runtime_genres g ON g.id=c.genre_id
        WHERE c.entry_id=NEW.id AND c.kind<>0 AND (SELECT active FROM metadata_runtime_state WHERE id=1)=1
        UNION ALL
        SELECT CASE h.field ''' + ' '.join(f"WHEN '{name}' THEN {CODES[name]}" for name in FIELDS) + ''' END,
               COALESCE(o.number_value,o.text_value), o.genre_key
        FROM metadata_choice_heads h JOIN metadata_observations o ON o.id=h.observation_id
        WHERE h.entry_id=NEW.id AND o.value_kind<>'withdrawn' AND (SELECT active FROM metadata_runtime_state WHERE id=1)=0
    ), chosen AS (SELECT '''
    aggregate = []
    for field in FIELDS:
        code = CODES[field]
        aggregate.extend((f'MAX(field={code}) AS has_{field}', f'MAX(value) FILTER (WHERE field={code}) AS {field}'))
    aggregate.append(f"MAX(genre_key) FILTER (WHERE field={CODES['genre']}) AS genre_key")
    values = []
    columns = track_sources.SOURCE_COLUMNS if source else COLUMNS
    for column in columns:
        field = column.removeprefix('effective_')
        if field == 'genre_key':
            values.append('CASE WHEN c.has_genre THEN c.genre_key ELSE r.genre_key END')
        elif field == 'unresolved_genre':
            values.append('CASE WHEN c.has_genre THEN CASE WHEN c.genre_key IS NULL THEN c.genre END ELSE r.unresolved_genre END')
        else:
            fallback = f'r.{field}' if field in metadata_values.RECORDING_FIELDS else f'COALESCE(y.{field},u.{field})'
            if field == 'available':
                fallback = f'CASE WHEN NEW.video_id IS NULL THEN 0 ELSE {fallback} END'
            if source:
                fallback = fallback.replace(f'COALESCE(y.{field},u.{field})', f's.{field}')
            values.append(f'CASE WHEN c.has_{field} THEN c.{field} ELSE {fallback} END')
    if source:
        return (choices + ','.join(aggregate) + ' FROM choices) SELECT ' + ','.join(values)
                + ' FROM chosen c JOIN track_sources s ON s.id=NEW.id')
    return (choices + ','.join(aggregate) + ' FROM choices) SELECT ' + ','.join(values) +
        ' FROM chosen c JOIN recordings r ON r.id=NEW.recording_id '
        'LEFT JOIN youtube_items y ON y.video_id=NEW.video_id LEFT JOIN unresolved_items u ON u.entry_id=NEW.id')


def refresh_trigger():
    return ('CREATE TRIGGER catalog_metadata_refresh '
        'AFTER UPDATE OF effective_ready,recording_id,video_id ON catalog_entries BEGIN '
        'UPDATE catalog_entries SET (' + ','.join(COLUMNS) + ')=(' + _refresh_values() + ') WHERE id=NEW.id; END')


def _triggers():
    yield 'catalog_metadata_refresh', refresh_trigger()
    sources = (
        ('catalog_entries', 'id', 'id', (), ('INSERT',)),
        ('recordings', 'recording_id', 'id',
         ('genre_key', 'unresolved_genre', *(field for field in metadata_values.RECORDING_FIELDS if field != 'genre')), ('UPDATE',)),
        ('youtube_items', 'video_id', 'video_id', metadata_values.ITEM_FIELDS, ('UPDATE',)),
        ('unresolved_items', 'id', 'entry_id', metadata_values.ITEM_FIELDS, ('INSERT', 'UPDATE', 'DELETE')),
        ('metadata_choice_heads', 'id', 'entry_id', ('observation_id',), ('INSERT', 'UPDATE', 'DELETE')),
        ('metadata_runtime_choices', 'id', 'entry_id', ('revision',), ('INSERT', 'UPDATE', 'DELETE')),
    )
    for table, target, key, fields, events in sources:
        for event in events:
            name = f'catalog_metadata_{table}_{event.lower()}'
            change = ' OF ' + ','.join(fields) if event == 'UPDATE' else ''
            changed = ' WHEN ' + ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields) if change else ''
            subject = 'OLD' if event == 'DELETE' else 'NEW'
            yield name, (f'CREATE TRIGGER {name} AFTER {event}{change} ON {table}{changed} BEGIN '
                         f'UPDATE catalog_entries SET effective_ready=1 WHERE {target}={subject}.{key}; END')
    for event in ('UPDATE', 'DELETE'):
        name = 'catalog_metadata_audit_' + event.lower()
        yield name, (f'CREATE TRIGGER {name} BEFORE {event} ON catalog_metadata_audit '
                     "BEGIN SELECT RAISE(ABORT,'Catalog metadata audit is append-only'); END")


def _objects():
    yield 'catalog_metadata_state', STATE
    yield 'catalog_metadata_audit', AUDIT
    yield 'ix_catalog_metadata_pending', PENDING
    yield from _triggers()


def install(conn, _context):
    from . import listening_summaries

    conn.execute('ALTER TABLE catalog_entries ADD COLUMN effective_ready INTEGER NOT NULL DEFAULT 0')
    for column, affinity in COLUMNS.items():
        conn.execute(f'ALTER TABLE catalog_entries ADD COLUMN {column} {affinity}')
    for _, sql in _objects():
        conn.execute(sql)
    conn.execute('DROP TRIGGER listening_summary_catalog_entries_update')
    conn.execute(listening_summaries.CATALOG_LINK_UPDATE)
    conn.execute('INSERT INTO catalog_metadata_state VALUES (1,0)')
    # Empty schema compilation can publish the view immediately. Populated
    # databases reach the track-source migration using the release view.
    if not conn.execute('SELECT 1 FROM catalog_entries WHERE effective_ready=0 LIMIT 1').fetchone():
        conn.execute('DROP VIEW catalog_tracks')
        conn.execute(track_view(conn, canonical=False))
        conn.execute('DROP VIEW entry_field_choices')
        conn.execute('UPDATE catalog_metadata_state SET ready=1 WHERE id=1')


def validate(conn, *, canonical):
    expected = dict(_objects())
    observed = definitions(conn, (*expected, 'catalog_metadata_state', 'catalog_tracks', 'entry_field_choices'))
    for name, sql in expected.items():
        if name not in observed or tokens(sql) != tokens(observed[name]):
            raise MigrationError('Incompatible catalog metadata object: ' + name)
    columns = {row[1]: row[2] for row in conn.execute('PRAGMA main.table_info(catalog_entries)')}
    if not {'effective_ready': 'INTEGER', **COLUMNS}.items() <= columns.items():
        raise MigrationError('Missing typed catalog metadata columns')
    if ready(conn) and ('entry_field_choices' in observed or
                        tokens(observed.get('catalog_tracks', '')) != tokens(track_view(conn, canonical=canonical))):
        raise MigrationError('Incompatible effective catalog view')


def audit(conn, entry, recording, field, source, operation, value, at, *, genre_key=None):
    metadata_history.append(conn, entry, recording, field, source, operation, value, at, genre_key=genre_key)
