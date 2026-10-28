"""Cheap source revision fingerprints shared by summary workers and readers."""
from yt_playlist.repos.listening_analytics import PLAYER_KINDS

# Polling reads connection-local revision counters, not the library itself.
CATALOG = {'recordings', 'youtube_items', 'catalog_entries', 'library_memberships',
           'entry_field_choices', 'track_artists', 'artists', 'artist_redirects'}
PLAYLISTS = {'playlists', 'playlist_items', 'unresolved_playlist_items', 'playlist_scopes',
             'playlist_group', 'playlist_provenance', 'hidden_playlists', 'identities'}
SOURCES = {
    'trends': CATALOG | PLAYLISTS | {'play_events', 'play_event_calendar',
        'listening_observations', 'rec_feedback', 'album_tracks', 'albums',
        'album_library_memberships', 'album_provider_ids'},
    'cleanup': CATALOG | PLAYLISTS | {'cleanup_ignored', 'suppressed_overlaps',
        'overlap_ignored', 'overlap_kept', 'ignored_merges'},
    'analytics': CATALOG | PLAYLISTS | {'play_events', 'listening_observations', 'rec_feedback',
                                      'listening_rating_actions'},
    'model_health': CATALOG | PLAYLISTS | {'rec_vectors', 'play_events', 'play_event_calendar',
        'play_event_sources', 'listening_observations'},
}
SURFACES = {'trends': 'trend_rollups', 'cleanup': 'cleanup', 'analytics': 'listening_analytics',
            'model_health': 'model_health'}
DEPENDENCIES = {kind: {(table, '') for table in tables} for kind, tables in SOURCES.items()}
DEPENDENCIES['trends'].add(('settings', 'key:takeout_imported_at'))
DEPENDENCIES['analytics'].update(('player_events', 'key:' + kind) for kind in PLAYER_KINDS)
DEPENDENCIES['analytics'].add(('settings', 'key:radio_session_playlist_ids'))
DEPENDENCIES['analytics'].add(('settings', 'key:listening_timezone'))
DEPENDENCIES['model_health'].update(('settings', 'key:' + key) for key in (
    'rec_embed_method', 'rec_dim', 'rec_cooc_weighting', 'rec_param:max_track_minutes'))


def inputs(store, kind, now):
    return input_fingerprints(store, (kind,), now)[kind]


def input_fingerprints(store, kinds, now):
    """Capture just the summaries' dependencies once under the serving lock."""
    dependencies = set().union(*(DEPENDENCIES[kind] for kind in kinds))
    with store._lock:
        revisions = store._snapshot_revisions.capture(dependencies)
        # Importers can write through another connection. Conservatively refresh
        # on their commits; ordinary playback writes use this connection and only
        # relevant source changes invalidate these jobs.
        external = store.conn.execute('PRAGMA data_version').fetchone()[0]
        placeholders = ','.join('?' for _ in kinds)
        present = {row[0] for row in store.conn.execute(
            f'SELECT surface FROM rec_proposals WHERE surface IN ({placeholders})',
            tuple(SURFACES[kind] for kind in kinds))}
        if 'analytics' in kinds:
            from yt_playlist.rec.listening_dates import edition_day, timezone_name
            zone = timezone_name(store.settings.get_setting('listening_timezone'))
            editorial_clock = (edition_day(now, zone), int(now // 21600))
    return {kind: (
        tuple(sorted((table, revision) for (table, key), revision in revisions.items()
                     if (table, key) in DEPENDENCIES[kind])),
        external, editorial_clock if kind == 'analytics' else int(now // 86400) if kind in {'trends', 'model_health'} else None,
        SURFACES[kind] in present,
    ) for kind in kinds}
