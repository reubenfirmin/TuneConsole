"""Discovery candidate state on the catalog's recording and artist owners."""
from .schema_inspection import definitions


TRACK_INDEX = 'ix_recording_discovery_candidate'
ARTIST_INDEX = 'ix_artist_discovery_candidate'
ARTIST_FIELDS = ('score', 'thumbnail', 'found_at', 'last_shown', 'first_shown', 'offered_count',
                 'unresolved_genre', 'genre_key', 'recovery_filename', 'recovery_digest', 'recovery_bytes')


def tracks_folded(conn):
    return bool(definitions(conn, (TRACK_INDEX,)))


def track(conn, recording_id):
    return conn.execute('''
        SELECT id AS recording_id,discovery_entry_id AS entry_id,discovery_found_at AS found_at,
               discovery_first_shown AS first_shown,discovery_last_shown AS last_shown,
               discovery_offered_count AS offered_count
        FROM recordings WHERE id=? AND discovery_entry_id IS NOT NULL
    ''', (recording_id,)).fetchone()


def admit_track(conn, recording_id, entry_id, found_at, *, merge=False):
    if merge:
        conn.execute('''
            UPDATE recordings SET discovery_entry_id=coalesce(discovery_entry_id,?),
                discovery_found_at=min(coalesce(discovery_found_at,?),coalesce(?,discovery_found_at))
            WHERE id=?
        ''', (entry_id, found_at, found_at, recording_id))
    else:
        conn.execute('UPDATE recordings SET discovery_entry_id=?,discovery_found_at=? '
                     'WHERE id=? AND discovery_entry_id IS NULL', (entry_id, found_at, recording_id))


CLEAR_TRACK = 'discovery_entry_id=NULL,discovery_found_at=NULL,' \
              'discovery_first_shown=NULL,discovery_last_shown=NULL,discovery_offered_count=0'


def clear_track(conn, recording_id):
    conn.execute(f'UPDATE recordings SET {CLEAR_TRACK} WHERE id=? AND discovery_entry_id IS NOT NULL',
                 (recording_id,))


def artists_folded(conn):
    return bool(definitions(conn, (ARTIST_INDEX,)))


CLEAR_ARTIST = 'discovery_candidate=0,' + ','.join(
    f'discovery_{field}=' + ('0' if field == 'offered_count' else 'NULL') for field in ARTIST_FIELDS)


def clear_artist(conn, artist_id):
    return conn.execute(f'UPDATE artists SET {CLEAR_ARTIST} WHERE id=? AND discovery_candidate=1',
                        (artist_id,)).rowcount


def merge_artist(conn, previous, target, candidate):
    clear_artist(conn, previous)
    fields = ','.join(f'discovery_{field}=?' for field in ARTIST_FIELDS)
    conn.execute(f'UPDATE artists SET discovery_candidate=1,{fields} WHERE id=?',
                 (*(candidate[field] for field in ARTIST_FIELDS), target))


def admit_artist(conn, artist_id, score, thumbnail, found_at, *, refresh_score=False):
    conn.execute('''
        UPDATE artists SET discovery_candidate=1,
            discovery_score=CASE WHEN discovery_candidate=0 OR ? THEN ? ELSE discovery_score END,
            discovery_found_at=CASE WHEN discovery_candidate=0 THEN ? ELSE discovery_found_at END,
            discovery_thumbnail=CASE WHEN discovery_candidate=0 THEN ?
                ELSE coalesce(nullif(?,''),discovery_thumbnail) END
        WHERE id=?
    ''', (refresh_score, score, found_at, thumbnail, thumbnail, artist_id))
