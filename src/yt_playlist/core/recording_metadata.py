"""Maintain the selected metadata and artist credits of each recording."""
from . import catalog_metadata, listening_summaries, metadata_choice_selections, metadata_history, metadata_values
from . import runtime_metadata_choices
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions


CANONICAL_FIELDS = metadata_values.CANONICAL_FIELDS
CANONICAL_COLUMNS = {
    **{f'effective_{field}': catalog_metadata.COLUMNS[f'effective_{field}']
       for field in CANONICAL_FIELDS if field != 'genre'},
    'effective_genre_key': 'TEXT',
    'effective_unresolved_genre': 'TEXT',
}
SELECTION_COLUMNS = {
    **CANONICAL_COLUMNS,
    'effective_album': 'TEXT',
    'effective_duration_s': 'INTEGER',
    'effective_mb_year': 'TEXT',
    'playback_video_id': 'TEXT',
    'playback_is_discovered': 'INTEGER',
}
COLUMNS = {**SELECTION_COLUMNS, 'primary_credit_artist_id': 'INTEGER'}
STATE = '''CREATE TABLE recording_metadata_state (
    id INTEGER PRIMARY KEY CHECK(id=1), last_recording_id INTEGER NOT NULL,
    ready INTEGER NOT NULL CHECK(ready IN (0,1))
)'''
CREDITS = '''CREATE TABLE recording_additional_artists (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL,
    PRIMARY KEY(recording_id,artist_id)
) WITHOUT ROWID'''
OVERRIDES = "'edit','resolve','reset'"
AUDIT_INDEX = '''CREATE INDEX ix_recording_metadata_decisions
    ON catalog_metadata_audit(recording_id,field,id DESC) WHERE operation IN (''' + OVERRIDES + ')'
SOURCE_AUDIT_INDEX = '''CREATE INDEX ix_recording_metadata_source_decisions
    ON catalog_metadata_audit(entry_id,field) WHERE operation IN (''' + OVERRIDES + ')'


def installed(conn):
    return bool(definitions(conn, ('recording_metadata_state',)))


def ready(conn):
    return bool(conn.execute('SELECT ready FROM recording_metadata_state WHERE id=1').fetchone()[0])


def uses_canonical_view(conn):
    return installed(conn) and ready(conn)


def has_choice(conn, entry, field):
    from .db_revisions import setting_reads
    from .metadata_contract import ACTIVE

    choices = live_choice_sql(_choice_inputs('track_sources', owned_history=True))
    with setting_reads(conn, (ACTIVE,)):
        row = conn.execute(f'''SELECT EXISTS(WITH {choices} SELECT 1 FROM choices WHERE field=?)
            FROM recordings WHERE id=(SELECT recording_id FROM track_sources WHERE id=?)''', (field, entry)).fetchone()
    return bool(row and row[0])


def live_choice_sql(sql):
    from .metadata_contract import ACTIVE_SQL, PREDECESSOR_ACTIVE_SQL, with_genre_keys

    return metadata_choice_selections.upgrade_selection_sql(with_genre_keys(sql)
        .replace(PREDECESSOR_ACTIVE_SQL, ACTIVE_SQL)
        .replace('metadata_runtime_choices', runtime_metadata_choices.VIEW))


def _choice_inputs(source='catalog_entries', *, owned_history=False):
    fields = ','.join(f"'{field}'" for field in CANONICAL_FIELDS)
    codes = ','.join(str(catalog_metadata.CODES[field]) for field in CANONICAL_FIELDS)
    field_names = ' '.join(f"WHEN {catalog_metadata.CODES[field]} THEN '{field}'" for field in CANONICAL_FIELDS)
    corrections = f'''SELECT a.field,a.value,a.genre_key,a.operation,
                       ROW_NUMBER() OVER (PARTITION BY a.field ORDER BY a.id DESC) AS priority
                FROM catalog_metadata_audit a
                WHERE a.recording_id=recordings.id AND a.operation IN ({OVERRIDES})
                  AND a.field IN ({fields})'''
    uncorrected = f'''NOT EXISTS (SELECT 1 FROM catalog_metadata_audit a WHERE a.entry_id=s.id
                              AND a.field=CASE c.field {field_names} END AND a.operation IN ({OVERRIDES}))'''
    if owned_history:
        corrections = f'''SELECT event.value->>2 AS field,{metadata_history.value_sql('event.value')} AS value,
                    event.value->>6 AS genre_key,event.value->>4 AS operation,
                    ROW_NUMBER() OVER (PARTITION BY event.value->>2 ORDER BY event.value->>0 DESC) AS priority
                FROM json_each(recordings.{metadata_history.CORRECTIONS}) event WHERE (event.value->>2) IN ({fields})'''
        uncorrected = f'(s.{metadata_history.CORRECTED_FIELDS}->>CASE c.field {field_names} END) IS NULL'
    return f'''
        current_choices AS (
            SELECT field,value,genre_key,operation FROM (
                {corrections}
            ) WHERE priority=1
        ), installed_choices AS (
            SELECT CASE c.field {field_names} END AS field,
                   COALESCE(c.number_value,c.text_value) AS value, g.genre_key,
                   c.id AS sequence
            FROM {source} s JOIN metadata_runtime_choices c ON c.entry_id=s.id
            LEFT JOIN metadata_runtime_genres g ON g.id=c.genre_id
            WHERE s.recording_id=recordings.id AND c.kind<>0 AND c.field IN ({codes})
              AND (SELECT active FROM metadata_runtime_state WHERE id=1)=1
              AND {uncorrected}
            UNION ALL
            SELECT h.field,COALESCE(o.number_value,o.text_value),o.genre_key,o.id
            FROM {source} s JOIN metadata_choice_heads h ON h.entry_id=s.id
            JOIN metadata_observations o ON o.id=h.observation_id
            WHERE s.recording_id=recordings.id AND o.recording_id=recordings.id
              AND o.value_kind<>'withdrawn' AND h.field IN ({fields})
              AND (SELECT active FROM metadata_runtime_state WHERE id=1)=0
        ), choices AS (
            SELECT field,value,genre_key FROM current_choices WHERE operation<>'reset'
            UNION ALL
            SELECT field,value,genre_key FROM (
                SELECT h.*,ROW_NUMBER() OVER (PARTITION BY h.field ORDER BY h.sequence DESC) AS priority
                FROM installed_choices h WHERE NOT EXISTS(SELECT 1 FROM current_choices a WHERE a.field=h.field)
            ) WHERE priority=1
        )'''


def _resolved_values(reset_field, folded):
    aggregates = []
    values = []
    for field in CANONICAL_FIELDS:
        present = f"COUNT(*) FILTER (WHERE field='{field}')"
        if field == reset_field:
            present = '0'
        aggregates.extend((f'{present} AS has_{field}',
                           f"MAX(value) FILTER (WHERE field='{field}') AS {field}"))
        if field == 'genre':
            aggregates.append("MAX(genre_key) FILTER (WHERE field='genre') AS genre_key")
            continue
        base = f'recordings.{field}'
        if field in ('title', 'artist'):
            fallback = (f'ELSE first.{field}' if folded else
                        f'WHEN first.video_id IS NOT NULL THEN y.{field} ELSE u.{field}')
            base = f'CASE WHEN first.id IS NULL THEN {base} {fallback} END'
        values.append(f'CASE WHEN c.has_{field} THEN c.{field} ELSE {base} END')
    values.extend(('CASE WHEN c.has_genre THEN c.genre_key ELSE recordings.genre_key END',
                   'CASE WHEN c.has_genre THEN CASE WHEN c.genre_key IS NULL THEN c.genre END '
                   'ELSE recordings.unresolved_genre END'))
    return ','.join(aggregates), ','.join(values)


def _refresh(target, *, reset_field=None, folded=False, preparation_table=None, owned_history=False):
    aggregates, values = _resolved_values(reset_field, folded)
    if folded:
        source = 'track_sources'
        first_source = 'SELECT id,title,artist FROM track_sources WHERE recording_id=recordings.id ORDER BY id LIMIT 1'
        table = 'recordings'
        if preparation_table is not None:
            table = f'"{preparation_table}" AS recordings'
            source = 'catalog_entries'
            first_source = '''SELECT e.id, COALESCE(y.title,u.title) title, COALESCE(y.artist,u.artist) artist
                FROM catalog_entries e LEFT JOIN youtube_items y ON y.video_id=e.video_id
                LEFT JOIN unresolved_items u ON u.entry_id=e.id
                WHERE e.recording_id=recordings.id ORDER BY e.id LIMIT 1'''
        return f'''UPDATE {table} SET ({','.join(CANONICAL_COLUMNS)})=(
            WITH first_source AS ({first_source}), {_choice_inputs(source, owned_history=owned_history)},
            canonical AS (SELECT {aggregates} FROM choices)
            SELECT {values} FROM canonical c LEFT JOIN first_source first ON 1
        ) WHERE id IN ({target});'''
    return f'''UPDATE recordings SET ({','.join(SELECTION_COLUMNS)})=(
        WITH library AS (
            SELECT MIN(e.id) FILTER (WHERE y.video_id IS NOT NULL AND y.available IS NOT 0) AS playable,
                   MIN(e.effective_mb_year) FILTER (
                       WHERE e.effective_mb_year GLOB '[0-9][0-9][0-9][0-9]*') AS year
            FROM catalog_entries e
            JOIN library_memberships m ON m.entry_id=e.id
            LEFT JOIN youtube_items y ON y.video_id=e.video_id
            WHERE e.recording_id=recordings.id AND e.effective_ready=1
        ), discovery AS (
            SELECT e.id
            FROM discovery_memberships m
            JOIN catalog_entries e ON e.id=m.entry_id
            WHERE m.recording_id=recordings.id AND e.recording_id=recordings.id
              AND e.effective_ready=1 AND e.video_id IS NOT NULL AND e.effective_available IS NOT 0
        ), first_source AS (
            SELECT id,video_id FROM catalog_entries
            WHERE recording_id=recordings.id AND effective_ready=1 ORDER BY id LIMIT 1
        ), {_choice_inputs()},
        canonical AS (SELECT {aggregates} FROM choices)
        SELECT {values}, e.effective_album,e.effective_duration_s,
               CASE WHEN l.playable IS NULL THEN e.effective_mb_year ELSE l.year END,
               CASE WHEN l.playable IS NOT NULL OR d.id IS NOT NULL THEN e.video_id END,
               l.playable IS NULL AND d.id IS NOT NULL
        FROM canonical c CROSS JOIN library l LEFT JOIN discovery d ON l.playable IS NULL
        LEFT JOIN first_source first ON 1
        LEFT JOIN catalog_entries e ON e.id=COALESCE(l.playable,d.id,first.id)
        LEFT JOIN youtube_items y ON y.video_id=first.video_id
        LEFT JOIN unresolved_items u ON u.entry_id=first.id
    ) WHERE id IN ({target});'''


def _refresh_credits(target, source='catalog_entries', *, recordings='recordings',
                     additional='recording_additional_artists'):
    return (
        f'''UPDATE {recordings} SET primary_credit_artist_id=(
            SELECT MIN(a.artist_id) FROM {source} e JOIN entry_artists a ON a.entry_id=e.id
            WHERE e.recording_id={recordings}.id
        ) WHERE id IN ({target})''',
        f'DELETE FROM {additional} WHERE recording_id IN ({target})',
        f'''INSERT INTO {additional}(recording_id,artist_id)
        SELECT DISTINCT e.recording_id,a.artist_id
        FROM {source} e JOIN entry_artists a ON a.entry_id=e.id
        JOIN {recordings} r ON r.id=e.recording_id
        WHERE e.recording_id IN ({target}) AND a.artist_id<>r.primary_credit_artist_id''',
    )


def triggers(*, folded=False):
    from . import track_sources

    source = 'track_sources' if folded else 'catalog_entries'
    def trigger(name, event, table, statements, when=''):
        return name, f'CREATE TRIGGER {name} AFTER {event} ON {table} {when} BEGIN {statements} END'

    def refresh_credits(target):
        return '; '.join(_refresh_credits(target, source)) + ';'

    def refresh(target):
        return _refresh(target, folded=folded)

    columns = track_sources.SOURCE_COLUMNS if folded else catalog_metadata.COLUMNS
    yield trigger('recording_metadata_entry_values', 'UPDATE OF ' + ','.join(columns),
                  source, refresh('NEW.recording_id'), '' if folded else 'WHEN NEW.effective_ready=1')
    if folded:
        provider_fields = ('title', 'artist', 'genre_key', 'unresolved_genre',
                           *(field for field in metadata_values.RECORDING_FIELDS if field != 'genre'))
        yield trigger('recording_metadata_provider_values', 'UPDATE OF ' + ','.join(provider_fields),
                      'recordings', refresh('NEW.id'))
    yield trigger('recording_metadata_entry_moved', 'UPDATE OF recording_id', source,
                  refresh('OLD.recording_id, NEW.recording_id' if folded else 'OLD.recording_id')
                  + refresh_credits('OLD.recording_id, NEW.recording_id'),
                  'WHEN OLD.recording_id IS NOT NEW.recording_id')
    yield trigger('recording_metadata_entry_removed', 'DELETE', source,
                  refresh('OLD.recording_id') + refresh_credits('OLD.recording_id'))
    fields = ','.join(f"'{field}'" for field in CANONICAL_FIELDS)
    yield trigger('recording_metadata_audit_insert', 'INSERT', 'catalog_metadata_audit',
                  refresh('NEW.recording_id'),
                  f'WHEN NEW.operation IN ({OVERRIDES}) AND NEW.field IN ({fields})')

    if not folded:
        for event, row in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
            yield trigger('recording_metadata_library_' + event.lower(), event, 'library_memberships',
                          refresh(f'SELECT recording_id FROM {source} WHERE id={row}.entry_id'))
        yield trigger('recording_metadata_library_update', 'UPDATE OF entry_id', 'library_memberships',
                      refresh(f'SELECT recording_id FROM {source} WHERE id IN (OLD.entry_id, NEW.entry_id)'),
                      'WHEN OLD.entry_id IS NOT NEW.entry_id')

        for event, target in (('INSERT', 'NEW.recording_id'), ('DELETE', 'OLD.recording_id'),
                              ('UPDATE OF entry_id,recording_id', 'OLD.recording_id, NEW.recording_id')):
            yield trigger('recording_metadata_discovery_' + event.split()[0].lower(), event, 'discovery_memberships',
                          refresh(target))

    for event, target in (
        ('INSERT', 'id=NEW.entry_id'),
        ('DELETE', 'id=OLD.entry_id'),
        ('UPDATE OF artist_id,entry_id', 'id IN (OLD.entry_id,NEW.entry_id)'),
    ):
        yield trigger('recording_metadata_credit_' + event.split()[0].lower(), event, 'entry_artists',
                      refresh_credits(f'SELECT recording_id FROM {source} WHERE ' + target))

    changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in CANONICAL_COLUMNS)
    yield trigger('recording_metadata_preview_invalidate', 'UPDATE OF ' + ','.join(CANONICAL_COLUMNS), 'recordings',
                  "UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0 AND EXISTS("
                  "SELECT 1 FROM group_preview_dependencies WHERE table_name='recordings' "
                  "AND record_key IN ('','group:catalog','key:'||NEW.identity_key));", 'WHEN ' + changed)


def _objects():
    yield 'recording_metadata_state', STATE
    yield 'recording_additional_artists', CREDITS
    yield 'ix_recording_metadata_decisions', AUDIT_INDEX
    yield 'ix_recording_metadata_source_decisions', SOURCE_AUDIT_INDEX
    yield 'listening_summary_recordings_update', listening_summaries.RECORDING_IDENTITY_UPDATE
    yield from triggers()


def install(conn, _context):
    for column, declaration in COLUMNS.items():
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {column} {declaration}')
    conn.execute('DROP TRIGGER listening_summary_recordings_update')
    for _, sql in _objects():
        conn.execute(sql)
    conn.execute('INSERT INTO recording_metadata_state VALUES (1, 0, NOT EXISTS(SELECT 1 FROM recordings))')
    if ready(conn) and catalog_metadata.ready(conn):
        publish_catalog(conn)


def validate(conn):
    expected = dict(_objects())
    observed = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in observed or tokens(sql) != tokens(observed[name]):
            raise MigrationError('Incompatible recording metadata object: ' + name)
    columns = {row[1]: row[2] for row in conn.execute('PRAGMA main.table_info(recordings)')}
    if not {name: declaration.split()[0] for name, declaration in COLUMNS.items()}.items() <= columns.items():
        raise MigrationError('Missing recording metadata columns')
    if conn.execute('SELECT ready FROM recording_metadata_state WHERE id=1').fetchone() is None:
        raise MigrationError('Missing recording metadata state')


def publish_catalog(conn):
    conn.execute('DROP VIEW catalog_tracks')
    conn.execute(catalog_metadata.track_view(conn, canonical=True))


def reset_value(conn, recording, field):
    # Restore this field's selected provider value before appending the reset.
    # The audit insert refreshes the row under the same write reservation.
    sql = _refresh('?', reset_field=field, folded=True, owned_history=True)
    conn.execute(live_choice_sql(sql), (recording,))
    if field == 'genre':
        return conn.execute(
            'SELECT COALESCE(g.name,r.effective_unresolved_genre),r.effective_genre_key '
            'FROM recordings r LEFT JOIN genre_catalog d ON d.key=r.effective_genre_key '
            'LEFT JOIN genre_catalog g ON g.key=COALESCE(d.redirect_key,r.effective_genre_key) WHERE r.id=?',
            (recording,)).fetchone()
    return conn.execute(f'SELECT effective_{field},NULL FROM recordings WHERE id=?', (recording,)).fetchone()


def audit_choice(conn, entry, recording, field, source, prepared, at):
    if prepared.kind != 'withdrawn':
        operation = 'resolve' if source == 'conflict_resolution' else 'edit'
        value = prepared.number if prepared.number is not None else prepared.text
        catalog_metadata.audit(conn, entry, recording, field, source, operation, value, at,
                               genre_key=prepared.genre_key)
        return
    if field in CANONICAL_FIELDS:
        value, genre_key = reset_value(conn, recording, field)
    else:
        value, genre_key = conn.execute(f'SELECT effective_{field},NULL FROM track_sources WHERE id=?', (entry,)).fetchone()
    catalog_metadata.audit(conn, entry, recording, field, source, 'reset', value, at, genre_key=genre_key)
