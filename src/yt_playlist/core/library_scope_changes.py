"""Queue content preparation when a recording's library evidence changes."""
from . import track_sources
from .catalog_query import library_recordings_sql
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions


RETIRED_VIEWS = ('playable_library_tracks', 'catalog_enrichment_tracks', 'tracks')
NAME = 'personal_library_scope'
ALBUM_STORAGE_INDEX = 'ix_album_canonical'
PLAYLIST_ITEMS_INDEX = 'ix_playlist_item_source'
UNATTRIBUTED_CHANGED = '(OLD.unattributed_saved_source IS NULL)<>(NEW.unattributed_saved_source IS NULL)'

_PREVIEW_COLUMNS = (
    'effective_album', 'effective_duration_s', 'effective_mb_year', 'playback_video_id',
    'playback_is_discovered', 'title_overridden', 'artist_overridden',
)
_PREVIEW_VIEWS = (*RETIRED_VIEWS, 'catalog_playback_tracks', 'youtube_items', 'discovered_tracks',
                  'recording_field_sources', 'item_field_sources', 'enrichment_conflict')
_PREVIEW_TRIGGERS = (
    'draft_entry_subject_insert', 'draft_entry_subject_update',
    'radio_queue_subject_insert', 'radio_queue_subject_update', 'group_preview_changed_track_sources_update',
    'recording_metadata_audit_insert', 'recording_metadata_entry_moved', 'recording_metadata_entry_removed',
    'recording_metadata_entry_values', 'recording_metadata_provider_values',
)


def applied(conn):
    return conn.execute('SELECT 1 FROM schema_changes WHERE name=?', (NAME,)).fetchone() is not None


def preview_layout(conn):
    if not track_sources.folded(conn):
        return False
    owners = definitions(conn, ('library_memberships', 'youtube_items', 'catalog_entries'))
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(track_sources)')}
    return (set(owners) == {'library_memberships', 'youtube_items'}
            and owners['youtube_items'].upper().startswith('CREATE VIEW ')
            and {'library_origin', 'library_added_at'}.isdisjoint(columns))


def enqueue_missing(conn, through, *, after=0):
    requested = 'SELECT id,identity_key FROM recordings WHERE id>? AND id<=?'
    conn.execute(
        'INSERT INTO rec_content_pending(identity_key) '
        f'SELECT l.identity_key FROM ({library_recordings_sql(requested, installed_redirect_table=True)}) l '
        'LEFT JOIN prepared_recording_inputs p ON p.recording_id=l.recording_id '
        'WHERE COALESCE(p.library_present,0)=0 ON CONFLICT(identity_key) DO NOTHING',
        (after, through))


def upgrade_preview(conn, *, metadata_active, metadata_definitions):
    """Finish the earlier source-table conversion in the caller's transaction."""
    from . import destination_schema, group_preview_work, metadata_runtime

    replaced = set(_PREVIEW_VIEWS) | set(_PREVIEW_TRIGGERS)
    for name, sql in definitions(conn, replaced).items():
        conn.execute(f'DROP {sql.split()[1]} "{name}"')
    conn.execute('ALTER TABLE track_sources ADD COLUMN library_origin TEXT')
    conn.execute('ALTER TABLE track_sources ADD COLUMN library_added_at REAL '
                 'CHECK(library_origin IS NOT NULL OR library_added_at IS NULL)')
    if conn.execute('SELECT 1 FROM library_memberships m LEFT JOIN track_sources e ON e.id=m.entry_id '
                    'WHERE e.id IS NULL LIMIT 1').fetchone():
        raise MigrationError('Saved membership has no track source')
    conn.execute('UPDATE track_sources SET (library_origin,library_added_at)=('
                 'SELECT origin,added_at FROM library_memberships WHERE entry_id=track_sources.id) '
                 'WHERE id IN (SELECT entry_id FROM library_memberships)')
    conn.execute('DROP TABLE library_memberships')
    for source in ('discovery', 'library'):
        for event in ('insert', 'update', 'delete'):
            conn.execute(f'DROP TRIGGER IF EXISTS recording_metadata_{source}_{event}')
    for name in _PREVIEW_COLUMNS:
        conn.execute(f'ALTER TABLE recordings DROP COLUMN {name}')

    restored = set()
    replaced.remove('enrichment_conflict')
    # The current destination has retired these guards. This intermediate
    # schema still needs the guards generated from its repaired source table.
    replaced.remove('group_preview_changed_track_sources_update')
    definition = destination_schema.definition()
    predecessor_objects = {
        words[2]: sql for section in ('discovery_track_predecessor_statements',
                                     'metadata_observation_predecessor_statements')
        for sql in definition[section] if (words := tokens(sql))[1] in ('view', 'trigger')
        and words[2] in replaced
    }
    for sql in destination_schema.statements():
        words = tokens(sql)
        if words[0] == 'create' and words[1] in ('view', 'trigger') and words[2] in replaced:
            conn.execute(predecessor_objects.get(words[2], sql))
            restored.add(words[2])
    # This preview upgrade still has the original metadata relation. The
    # catalog retirement change rewrites the view later in the same startup.
    conn.execute(next(sql for sql in metadata_definitions if sql.startswith('CREATE VIEW enrichment_conflict ')))
    restored.add('enrichment_conflict')
    if metadata_active:
        for name, sql in metadata_runtime.views(include_choices=False, entry_owner='track_sources').items():
            if name in restored:
                conn.execute(f'DROP VIEW {name}')
                conn.execute(sql)
    for _, sql in triggers(folded=True):
        conn.execute(sql)
    group_preview_work.install_guards(conn, sources={'track_sources'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE invalid=0')
    after = 0
    while True:
        ids = conn.execute('SELECT id FROM recordings WHERE id>? ORDER BY id LIMIT 4000', (after,)).fetchall()
        if not ids:
            break
        enqueue_missing(conn, ids[-1][0], after=after)
        after = ids[-1][0]


def _album_recordings(album):
    return ('SELECT o.recording_id FROM albums a JOIN album_track_occurrences o '
            f'ON o.tracklist_id=a.current_tracklist_id WHERE a.id IN ({album})')


def _playlist_albums(source):
    return (f'SELECT i.album_id FROM album_item_observations i WHERE i.entry_id IN ({source}) '
            'UNION SELECT t.album_id FROM album_occurrence_sources s '
            'JOIN album_track_occurrences o ON o.id=s.occurrence_id '
            'JOIN album_tracklists t ON t.id=o.tracklist_id '
            'JOIN albums a ON a.id=t.album_id AND a.current_tracklist_id=t.id '
            f'WHERE s.entry_id IN ({source})')


def _published_album(row):
    return ('SELECT a.id FROM album_track_occurrences o JOIN album_tracklists t ON t.id=o.tracklist_id '
            'JOIN albums a ON a.id=t.album_id AND a.current_tracklist_id=t.id '
            f'WHERE o.id={row}.occurrence_id')


def trigger_layout(conn):
    """Select the trigger variants for offline schema validation."""
    markers = {'album_folded': ALBUM_STORAGE_INDEX, 'listening_folded': 'ix_play_event_day',
               'playlist_items_folded': PLAYLIST_ITEMS_INDEX}
    installed = definitions(conn, markers.values())
    return {variant: index in installed for variant, index in markers.items()}


def triggers(*, folded=False, album_folded=False, listening_folded=False, playlist_items_folded=False):
    for table, subject in (
        ('play_events', lambda row: f'SELECT {row}.recording_id'),
        ('play_event_sources', lambda row: f'SELECT recording_id FROM play_events WHERE id={row}.event_id'),
        ('play_event_calendar', lambda row: f'SELECT recording_id FROM play_events WHERE id={row}.event_id'),
        ('listening_observations', lambda row: f'SELECT {row}.recording_id'),
        ('account_albums', lambda row: _album_recordings(f'SELECT {row}.album_id')),
        ('unattributed_album_memberships', lambda row: _album_recordings(f'SELECT {row}.album_id')),
        ('playlist_items', lambda row: _album_recordings(_playlist_albums(
            f'SELECT {row}.track_source_id' if playlist_items_folded
            else f'SELECT e.id FROM track_sources e WHERE e.video_id={row}.video_id'))),
        ('unresolved_playlist_items', lambda row: _album_recordings(_playlist_albums(f'SELECT {row}.entry_id'))),
        ('album_item_observations', lambda row: _album_recordings(
            f'SELECT {row}.album_id WHERE EXISTS (SELECT 1 FROM playlist_tracks p WHERE p.track_id={row}.entry_id)')),
        ('album_occurrence_sources', lambda row: _album_recordings(_published_album(row))),
    ):
        if album_folded and table == 'unattributed_album_memberships':
            continue
        if listening_folded and table in ('play_event_sources','play_event_calendar'):
            continue
        if playlist_items_folded and table == 'unresolved_playlist_items':
            continue
        for event, sides in (('INSERT', ('NEW',)), ('UPDATE', ('OLD', 'NEW')), ('DELETE', ('OLD',))):
            name = f'library_scope_{table}_{event.lower()}'
            if table == 'album_item_observations' and event == 'UPDATE':
                event = 'UPDATE OF album_id,entry_id'
            yield name, _trigger(name, event, table, ' UNION '.join(subject(side) for side in sides))
    if album_folded:
        name = 'library_scope_album_unattributed'
        yield name, _trigger(name, 'UPDATE OF unattributed_saved_source', 'albums',
                             _album_recordings('SELECT NEW.id'), UNATTRIBUTED_CHANGED)
    name = 'library_scope_album_publication'
    yield name, _trigger(name, 'UPDATE OF current_tracklist_id', 'albums',
        'SELECT o.recording_id FROM album_track_occurrences o '
        'WHERE o.tracklist_id IN (OLD.current_tracklist_id,NEW.current_tracklist_id)',
        'OLD.current_tracklist_id IS NOT NEW.current_tracklist_id')
    name = 'library_scope_album_occurrence'
    yield name, _trigger(name, 'UPDATE OF recording_id', 'album_track_occurrences',
        'SELECT OLD.recording_id UNION SELECT NEW.recording_id',
        'OLD.recording_id IS NOT NEW.recording_id AND EXISTS ('
        'SELECT 1 FROM albums WHERE current_tracklist_id=NEW.tracklist_id)')
    if folded:
        changes = (
            ('insert', 'INSERT', 'SELECT NEW.recording_id', 'NEW.library_origin IS NOT NULL'),
            ('delete', 'DELETE', 'SELECT OLD.recording_id', 'OLD.library_origin IS NOT NULL'),
            ('update', 'UPDATE OF library_origin,recording_id', 'SELECT OLD.recording_id UNION SELECT NEW.recording_id',
             '(OLD.library_origin IS NOT NULL OR NEW.library_origin IS NOT NULL) AND '
             '(OLD.library_origin IS NOT NEW.library_origin OR OLD.recording_id IS NOT NEW.recording_id)'),
        )
        for suffix, event, subjects, changed in changes:
            name = 'library_scope_explicit_' + suffix
            yield name, _trigger(name, event, 'track_sources', subjects, changed)


def _trigger(name, event, table, subjects, changed=None):
    when = f' WHEN {changed}' if changed else ''
    return (f'CREATE TRIGGER {name} AFTER {event} ON {table}{when} BEGIN '
            'INSERT INTO rec_content_pending(identity_key) SELECT r.identity_key '
            f'FROM recordings r WHERE r.id IN ({subjects}) '
            'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1; END')


def install(conn, _context):
    for name in RETIRED_VIEWS:
        conn.execute(f'DROP VIEW {name}')
    conn.execute('DROP VIEW track_sources')
    conn.execute('CREATE VIEW track_sources AS ' + track_sources.installed_projection())
    for _, sql in triggers():
        conn.execute(sql)


def validate(conn):
    from . import content_work

    if definitions(conn, RETIRED_VIEWS):
        raise MigrationError('Retired personal library views remain')
    folded = track_sources.folded(conn)
    expected = dict(triggers(folded=folded, **trigger_layout(conn)))
    if not folded:
        expected['track_sources'] = 'CREATE VIEW track_sources AS ' + track_sources.installed_projection()
    actual = definitions(conn, expected)
    if definitions(conn, (content_work.INDEX,)):
        expected = {name: content_work.upgrade_enqueue_sql(sql) for name, sql in expected.items()}
    if definitions(conn, (content_work.RECORDING_INDEX,)):
        expected = {name: content_work.upgrade_recording_enqueue_sql(sql) for name, sql in expected.items()}
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError(f'Incompatible personal library schema: {name}')
