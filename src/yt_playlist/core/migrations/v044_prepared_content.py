"""Capture changed content inputs without replacing the established learning models.

The pending queue survives restarts. Its sequence is independent of revisions so
a repeatedly changing recording cannot hold up every other recording.
"""
from .v034_progressive_learning import SOURCES


CONTENT_SOURCES = (
    ('recordings', 'identity_key', None),
    ('youtube_items', 'recording_id', 'recording'),
    ('catalog_entries', 'recording_id', 'recording'),
    ('unresolved_items', 'entry_id', 'entry'),
    ('library_memberships', 'entry_id', 'entry'),
    ('entry_field_choices', 'entry_id', 'entry'),
    ('discovery_memberships', 'recording_id', 'recording'),
)


def statements():
    yield ('CREATE TABLE rec_content_pending (position INTEGER PRIMARY KEY AUTOINCREMENT,'
           'identity_key TEXT NOT NULL UNIQUE,revision INTEGER NOT NULL DEFAULT 1)')
    yield ('CREATE TABLE rec_prepared_content (identity_key TEXT PRIMARY KEY NOT NULL,'
           'payload TEXT NOT NULL,digest TEXT NOT NULL,captured_at REAL NOT NULL,revision INTEGER NOT NULL)')
    yield 'CREATE INDEX ix_prepared_content_revision ON rec_prepared_content(revision)'
    yield ('CREATE TABLE rec_preparation (id INTEGER PRIMARY KEY CHECK(id=1),'
           'content_changes INTEGER NOT NULL DEFAULT 0,evidence_changes INTEGER NOT NULL DEFAULT 0)')
    yield 'INSERT INTO rec_preparation(id) VALUES (1)'
    yield 'INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings ORDER BY id'
    for table, column, lookup in CONTENT_SOURCES:
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            writes = []
            for side in sides:
                value = f'{side}.{column}'
                if lookup == 'recording':
                    value = f'(SELECT identity_key FROM recordings WHERE id={value})'
                elif lookup == 'entry':
                    value = ('(SELECT r.identity_key FROM catalog_entries e JOIN recordings r '
                             f'ON r.id=e.recording_id WHERE e.id={value})')
                writes.append('INSERT INTO rec_content_pending(identity_key) '
                              f'SELECT {value} WHERE {value} IS NOT NULL '
                              'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;')
            columns = next((cols for name, _kind, _key, cols in SOURCES if name == table), None)
            # Unresolved item metadata has the same selected fields as a YouTube item.
            if table == 'unresolved_items':
                columns = next(cols for name, _kind, _key, cols in SOURCES if name == 'youtube_items')[1:]
            elif table == 'entry_field_choices':
                columns = ('entry_id', 'field', 'value_json')
            elif table == 'discovery_memberships':
                columns = ('recording_id', 'entry_id')
            when = (' WHEN ' + ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
                    if event == 'UPDATE' and columns else '')
            yield (f'CREATE TRIGGER rec_content_{table}_{event.lower()} AFTER {event} ON {table}'
                   f'{when} BEGIN ' + ''.join(writes) + ' END')
    # Explicit evidence has a separate accumulation counter. Provider metadata
    # alone cannot repeatedly admit a collaborative/artist fit.
    metadata = {name for name, _column, _lookup in CONTENT_SOURCES}
    for table, _kind, _expression, columns in SOURCES:
        if table in metadata:
            continue
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            when = (' WHEN ' + ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
                    if event == 'UPDATE' else '')
            yield (f'CREATE TRIGGER rec_evidence_{table}_{event.lower()} AFTER {event} ON {table}{when} '
                   'BEGIN UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1; END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
