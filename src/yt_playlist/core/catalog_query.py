"""Shared catalog scope and coherent playback representatives."""


COLLECTION_ALBUMS_SQL = '''
    SELECT album_id FROM saved_album_memberships
    UNION
    SELECT at.album_id
    FROM playlist_tracks p JOIN album_tracks at ON at.entry_id=p.track_id
'''

EXPLICIT_TRACKS_SQL = (
    'SELECT t.* FROM catalog_tracks t JOIN track_sources e ON e.id=t.id WHERE e.library_origin IS NOT NULL'
)


def is_playable_sql(alias=''):
    prefix = alias + '.' if alias else ''
    return f"NULLIF(trim({prefix}video_id),'') IS NOT NULL AND COALESCE({prefix}available,1)<>0"


IS_PLAYABLE_SQL = is_playable_sql()


def library_recordings_sql(recordings_sql='SELECT id,identity_key FROM recordings', *, include_listening=True,
                           installed_redirect_table=False):
    """One row per recording, optionally restricted to a caller's recording set."""
    listens = ('OR EXISTS (SELECT 1 FROM eligible_listening_evidence l WHERE l.recording_id=r.id)'
               if include_listening else '')
    retired = ('SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id' if installed_redirect_table else
               'SELECT 1 FROM recordings d WHERE d.id=r.id AND d.redirect_recording_id IS NOT NULL')
    return f'''
        WITH collection_albums AS MATERIALIZED ({COLLECTION_ALBUMS_SQL})
        SELECT r.id recording_id,r.identity_key
        FROM ({recordings_sql}) r
        WHERE NOT EXISTS ({retired})
          AND (
            EXISTS (SELECT 1 FROM track_sources e WHERE e.recording_id=r.id AND e.library_origin IS NOT NULL)
            {listens}
            OR EXISTS (
                SELECT 1 FROM album_track_occurrences o
                JOIN albums a ON a.current_tracklist_id=o.tracklist_id
                JOIN collection_albums c ON c.album_id=a.id
                WHERE o.recording_id=r.id)
          )
    '''


def library_tracks_sql(recordings_sql='SELECT id,identity_key FROM recordings', *, include_listening=True):
    """Source rows for the personal library, including alternative uploads."""
    scope = library_recordings_sql(recordings_sql, include_listening=include_listening)
    return (f'SELECT t.*,({IS_PLAYABLE_SQL}) AS is_playable FROM catalog_tracks t '
            f'JOIN ({scope}) l ON l.recording_id=t.recording_id')


def enrichment_recordings_sql(recordings_sql='SELECT id,identity_key FROM recordings'):
    return f'''
        WITH requested AS ({recordings_sql}),
        library AS MATERIALIZED ({library_recordings_sql('SELECT id,identity_key FROM requested')})
        SELECT r.id AS recording_id,r.identity_key,l.recording_id IS NOT NULL in_library,
               d.discovery_entry_id IS NOT NULL in_discovery
        FROM requested r LEFT JOIN library l ON l.recording_id=r.id
        JOIN recordings d ON d.id=r.id
        WHERE (l.recording_id IS NOT NULL OR d.discovery_entry_id IS NOT NULL)
          AND d.redirect_recording_id IS NULL
    '''


def enrichment_tracks_sql(recordings_sql='SELECT id,identity_key FROM recordings'):
    """One catalog input per eligible recording, with library inputs preferred."""
    return f'''
        WITH scope AS ({enrichment_recordings_sql(recordings_sql)})
        SELECT t.*,s.recording_id queue_order,
               CASE WHEN s.in_library THEN 'library' ELSE 'discovery' END source,
               s.in_library,s.in_discovery,1 AS eligible_for_enrichment
        FROM scope s JOIN recordings d ON d.id=s.recording_id
        JOIN catalog_tracks t ON t.id=CASE WHEN s.in_library
            THEN (SELECT e.id FROM track_sources e WHERE e.recording_id=s.recording_id
                  ORDER BY (e.library_origin IS NOT NULL) DESC,e.id LIMIT 1)
            ELSE d.discovery_entry_id END
    '''


def playable_tracks_sql(eligible_sql=None, *, prefer_playback=False):
    """One complete source row per recording, using its first eligible playable upload.

    Scoped callers supply a SELECT of eligible tracks (with parameters bound by
    their caller). Never select a global representative and then filter away a
    recording whose other upload was eligible for this account, artist or release.
    """
    if eligible_sql is None:
        eligible_sql = library_tracks_sql()
    preferred = 'MIN(t.id) FILTER (WHERE s.playback_preferred=1),' if prefer_playback else ''
    return (f'WITH eligible AS ({eligible_sql}) SELECT e.* FROM eligible e '
            f'WHERE e.id IN (SELECT COALESCE({preferred}MIN(t.id) FILTER (WHERE s.library_origin IS NOT NULL),MIN(t.id)) '
            'FROM eligible t JOIN track_sources s ON s.id=t.id '
            f'WHERE {is_playable_sql("t")} '
            'GROUP BY t.recording_id)')


LIBRARY_RECORDINGS_SQL = library_recordings_sql()
LIBRARY_TRACKS_SQL = library_tracks_sql()
PLAYABLE_LIBRARY_SQL = playable_tracks_sql()
ENRICHMENT_RECORDINGS_SQL = enrichment_recordings_sql()
ENRICHMENT_TRACKS_SQL = enrichment_tracks_sql()
