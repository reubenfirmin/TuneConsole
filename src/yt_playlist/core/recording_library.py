"""Keep Library and Discovery membership facts on the recording that owns them."""
from . import library_scope_changes, listening_views
from .association_work import MARKER as INLINE_CORRECTIONS
from .catalog_query import COLLECTION_ALBUMS_SQL, is_playable_sql
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions

NAME = 'recording_library_membership'
LISTENING_PROBES_NAME = 'probe_library_listening_by_recording'
COLUMNS = ('has_saved_source', 'has_eligible_listen', 'in_collection_album')
DISCOVERY_NAME = 'recording_discovery_membership'
DISCOVERY_COLUMN = 'is_discovery'
STATE = '''CREATE TABLE recording_library_state (
    id INTEGER PRIMARY KEY CHECK(id=1), ready INTEGER NOT NULL CHECK(ready IN (0,1))
)'''
_IN_LIBRARY = '(r.has_saved_source OR r.has_eligible_listen OR r.in_collection_album)'
RECORDINGS_SQL = f'''
    SELECT r.id recording_id,r.identity_key FROM recordings r
    WHERE r.redirect_recording_id IS NULL AND {_IN_LIBRARY}
'''
ENRICHMENT_SQL = f'''
    SELECT r.id recording_id,r.identity_key,{_IN_LIBRARY} in_library,r.discovery_entry_id IS NOT NULL in_discovery
    FROM recordings r
    WHERE r.redirect_recording_id IS NULL AND ({_IN_LIBRARY} OR r.discovery_entry_id IS NOT NULL)
'''
_TRACKS_FROM_FLAGS_SQL = f'''
    SELECT t.*,({is_playable_sql('t')}) AS is_playable
    FROM catalog_tracks t JOIN recordings r ON r.id=t.recording_id
    WHERE r.redirect_recording_id IS NULL AND
'''
TRACKS_SQL = _TRACKS_FROM_FLAGS_SQL + _IN_LIBRARY
OWNED_TRACKS_SQL = _TRACKS_FROM_FLAGS_SQL + '(r.has_saved_source OR r.in_collection_album)'


def installed(conn):
    return bool(definitions(conn, ('recording_library_track_sources_insert',)))


def _prepare(conn):
    """Backfill and publish in the caller's write transaction."""
    inputs, names = listening_views.materialized()
    conn.execute(f'''
        WITH {inputs},
        saved AS MATERIALIZED (
            SELECT DISTINCT recording_id FROM track_sources WHERE library_origin IS NOT NULL
        ), heard AS MATERIALIZED (
            SELECT DISTINCT recording_id FROM {names['eligible_listening_evidence']} WHERE recording_id IS NOT NULL
        ), collection_albums AS MATERIALIZED ({COLLECTION_ALBUMS_SQL}),
        album_members AS MATERIALIZED (
            SELECT DISTINCT o.recording_id FROM collection_albums c JOIN albums a ON a.id=c.album_id
            JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id
        ), membership AS (
            SELECT r.id,s.recording_id IS NOT NULL AS saved,h.recording_id IS NOT NULL AS heard,
                   a.recording_id IS NOT NULL AS album
            FROM recordings r LEFT JOIN saved s ON s.recording_id=r.id
            LEFT JOIN heard h ON h.recording_id=r.id
            LEFT JOIN album_members a ON a.recording_id=r.id
        )
        UPDATE recordings SET (has_saved_source,has_eligible_listen,in_collection_album)=(m.saved,m.heard,m.album)
        FROM membership m WHERE recordings.id=m.id
          AND (has_saved_source<>m.saved OR has_eligible_listen<>m.heard OR in_collection_album<>m.album)
    ''')
    conn.execute('UPDATE recording_library_state SET ready=1 WHERE id=1')


def _subject(row):
    return (f'SELECT {row}.recording_id WHERE {row}.recording_id IS NOT NULL '
            f'UNION SELECT id FROM recordings WHERE identity_key={row}.unresolved_subject_key')


def _event_subject(row):
    return ('SELECT p.recording_id FROM play_events p WHERE p.id=' + row + '.event_id '
            'UNION SELECT r.id FROM play_events p JOIN recordings r ON r.identity_key=p.unresolved_subject_key '
            f'WHERE p.id={row}.event_id')


def _trigger(table, event, flag, evidence, subjects, when=None, *, name=None):
    name = name or f'recording_library_{table}_{event.split()[0].lower()}'
    guard = f' WHEN {when}' if when else ''
    return name, (f'CREATE TRIGGER {name} AFTER {event} ON {table}{guard} BEGIN '
                  f'UPDATE recordings SET {flag}=evidence.present FROM ('
                  f'SELECT r.id, EXISTS ({evidence}) AS present FROM recordings r WHERE r.id IN ({subjects})'
                  f') AS evidence WHERE recordings.id=evidence.id AND recordings.{flag}<>evidence.present; END')


def _changes(table, updates, flag, evidence, subject, *, when=None):
    yield _trigger(table, 'INSERT', flag, evidence, subject('NEW'), when and when('NEW'))
    yield _trigger(table, 'UPDATE OF ' + updates, flag, evidence,
                   subject('OLD') + ' UNION ' + subject('NEW'),
                   (f'({when("OLD")}) OR ({when("NEW")})' if when else None))
    yield _trigger(table, 'DELETE', flag, evidence, subject('OLD'), when and when('OLD'))


def _listening_triggers(*, folded, use_subject_probes):
    heard = 'has_eligible_listen'
    heard_evidence = (listening_views.recording_membership_evidence() if use_subject_probes else
                      'SELECT 1 FROM eligible_listening_evidence l WHERE l.recording_id=r.id')
    event_fields = 'account_id,recording_id,unresolved_subject_key,is_generated'
    if folded:
        event_fields += ',day,sources'
    yield from _changes('play_events', event_fields,
                        heard, heard_evidence, _subject)
    if not folded:
        yield from _changes('play_event_sources', 'event_id', heard, heard_evidence, _event_subject)
        yield from _changes('play_event_calendar', 'event_id,day', heard, heard_evidence, _event_subject)
    yield from _changes('listening_observations', 'account_id,recording_id,unresolved_subject_key,day,source',
                        heard, heard_evidence, _subject)
    yield _trigger('recordings', 'UPDATE OF identity_key', heard, heard_evidence, 'SELECT NEW.id',
                   'OLD.identity_key IS NOT NEW.identity_key')


def triggers(*, album_folded=False, listening_folded=False, playlist_items_folded=False, use_subject_probes=False):
    saved, _, album = COLUMNS
    saved_evidence = ('SELECT 1 FROM track_sources s WHERE s.recording_id=r.id '
                      'AND s.library_origin IS NOT NULL')
    yield from _changes('track_sources', 'library_origin,recording_id', saved, saved_evidence,
                        lambda row: f'SELECT {row}.recording_id',
                        when=lambda row: f'{row}.library_origin IS NOT NULL')
    yield from _listening_triggers(folded=listening_folded, use_subject_probes=use_subject_probes)

    album_evidence = '''
        SELECT 1 FROM album_track_occurrences o JOIN albums a ON a.current_tracklist_id=o.tracklist_id
        WHERE o.recording_id=r.id AND (
            EXISTS (SELECT 1 FROM saved_album_memberships m WHERE m.album_id=a.id)
            OR EXISTS (
                SELECT 1 FROM album_tracks t JOIN playlist_tracks p ON p.track_id=t.entry_id WHERE t.album_id=a.id
            )
        )'''
    album_recordings = library_scope_changes._album_recordings
    playlist_albums = library_scope_changes._playlist_albums
    tables = ('account_albums',) if album_folded else ('account_albums', 'unattributed_album_memberships')
    for table in tables:
        yield from _changes(table, 'album_id', album, album_evidence,
                            lambda row: album_recordings(f'SELECT {row}.album_id'))
    if album_folded:
        yield _trigger('albums', 'UPDATE OF unattributed_saved_source', album, album_evidence,
                       album_recordings('SELECT NEW.id'), library_scope_changes.UNATTRIBUTED_CHANGED,
                       name='recording_library_album_unattributed')
    if playlist_items_folded:
        yield from _changes('playlist_items', 'track_source_id', album, album_evidence,
                            lambda row: album_recordings(playlist_albums(f'SELECT {row}.track_source_id')))
    else:
        yield from _changes('playlist_items', 'video_id', album, album_evidence, lambda row: album_recordings(
            playlist_albums(f'SELECT id FROM track_sources WHERE video_id={row}.video_id')))
        yield from _changes('unresolved_playlist_items', 'entry_id', album, album_evidence,
                            lambda row: album_recordings(playlist_albums(f'SELECT {row}.entry_id')))
    yield from _changes('album_item_observations', 'album_id,entry_id', album, album_evidence, lambda row:
        album_recordings(f'SELECT {row}.album_id WHERE EXISTS ('
                         f'SELECT 1 FROM playlist_tracks WHERE track_id={row}.entry_id)'))
    yield from _changes('album_occurrence_sources', 'occurrence_id,entry_id', album, album_evidence,
                        lambda row: album_recordings(library_scope_changes._published_album(row)))
    yield from _changes('album_track_occurrences', 'tracklist_id,recording_id', album, album_evidence,
                        lambda row: f'SELECT {row}.recording_id UNION SELECT recording_id '
                        f'FROM album_track_occurrences WHERE tracklist_id={row}.tracklist_id',
                        when=lambda row: 'EXISTS (SELECT 1 FROM albums '
                        f'WHERE current_tracklist_id={row}.tracklist_id)')
    yield _trigger('albums', 'UPDATE OF current_tracklist_id', album, album_evidence,
                   'SELECT recording_id FROM album_track_occurrences '
                   'WHERE tracklist_id IN (OLD.current_tracklist_id,NEW.current_tracklist_id)',
                   'OLD.current_tracklist_id IS NOT NEW.current_tracklist_id')


def install(conn, _context):
    for column in COLUMNS:
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
    for _, sql in triggers():
        conn.execute(sql)
    conn.execute(STATE)
    conn.execute('INSERT INTO recording_library_state VALUES (1, NOT EXISTS(SELECT 1 FROM recordings))')
    if not conn.execute('SELECT ready FROM recording_library_state WHERE id=1').fetchone()[0]:
        _prepare(conn)


def validate(conn):
    use_subject_probes = conn.execute('SELECT 1 FROM schema_changes WHERE name=?',
                                     (LISTENING_PROBES_NAME,)).fetchone() is not None
    expected = dict(triggers(**library_scope_changes.trigger_layout(conn), use_subject_probes=use_subject_probes))
    folded = bool(definitions(conn, (INLINE_CORRECTIONS,)))
    if not folded:
        expected['recording_library_state'] = STATE
    _validate_objects(conn, expected)
    if not folded and conn.execute('SELECT ready FROM recording_library_state WHERE id=1').fetchone() is None:
        raise MigrationError('Missing recording Library membership state')


def _validate_objects(conn, expected):
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(sql) != tokens(actual[name]):
            raise MigrationError('Incompatible recording Library membership object: ' + name)


def install_listening_probes(conn, _context):
    for name, sql in _listening_triggers(folded=True, use_subject_probes=True):
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(sql)


def listening_probe_predecessors(conn):
    folded = library_scope_changes.trigger_layout(conn)['listening_folded']
    return (sql for _, sql in _listening_triggers(folded=folded, use_subject_probes=False))


def validate_listening_probes(conn):
    _validate_objects(conn, dict(_listening_triggers(folded=True, use_subject_probes=True)))


_DISCOVERY_TRIGGERS = {
    'recording_discovery_insert': '''AFTER INSERT ON discovery_memberships BEGIN
        UPDATE recordings SET is_discovery=1
        WHERE id=NEW.recording_id AND is_discovery=0 AND NOT (has_saved_source OR in_collection_album);
    END''',
    'recording_discovery_delete': '''AFTER DELETE ON discovery_memberships BEGIN
        UPDATE recordings SET is_discovery=0 WHERE id=OLD.recording_id AND is_discovery=1;
    END''',
    'recording_discovery_move': '''AFTER UPDATE OF recording_id ON discovery_memberships
    WHEN OLD.recording_id IS NOT NEW.recording_id BEGIN
        UPDATE recordings SET is_discovery=0 WHERE id=OLD.recording_id AND is_discovery=1;
        UPDATE recordings SET is_discovery=1
        WHERE id=NEW.recording_id AND is_discovery=0 AND NOT (has_saved_source OR in_collection_album);
    END''',
    'recording_discovery_collection': '''AFTER UPDATE OF has_saved_source,in_collection_album ON recordings
    WHEN (OLD.has_saved_source OR OLD.in_collection_album) <> (NEW.has_saved_source OR NEW.in_collection_album)
    BEGIN
        UPDATE recordings SET is_discovery=NOT (NEW.has_saved_source OR NEW.in_collection_album)
        FROM discovery_memberships d WHERE recordings.id=NEW.id AND d.recording_id=NEW.id;
    END''',
}


def discovery_installed(conn):
    from .discovery_candidates import TRACK_INDEX
    return bool(definitions(conn, ('recording_discovery_insert', TRACK_INDEX)))


def install_discovery(conn, _context):
    conn.execute(f'ALTER TABLE recordings ADD COLUMN {DISCOVERY_COLUMN} INTEGER NOT NULL DEFAULT 0')
    conn.execute('''UPDATE recordings SET is_discovery=1 FROM discovery_memberships d
                    WHERE recordings.id=d.recording_id AND NOT (has_saved_source OR in_collection_album)''')
    for name, body in _DISCOVERY_TRIGGERS.items():
        conn.execute(f'CREATE TRIGGER {name} {body}')


def validate_discovery(conn):
    from .discovery_candidates import tracks_folded
    if tracks_folded(conn):
        return
    actual = definitions(conn, _DISCOVERY_TRIGGERS)
    for name, body in _DISCOVERY_TRIGGERS.items():
        if name not in actual or tokens(actual[name]) != tokens(f'CREATE TRIGGER {name} {body}'):
            raise MigrationError('Incompatible recording Discovery membership object: ' + name)
