"""Summary dependencies and configuration composed from database facts."""
from yt_playlist.rec.listening_policy import PLAYER_KINDS

CATALOG = {'recordings', 'youtube_items', 'catalog_entries', 'library_memberships',
           'entry_field_choices', 'metadata_choice_heads', 'metadata_runtime_choices',
           'entry_artists', 'track_artists', 'artists', 'artist_redirects'}
PLAYLISTS = {'playlists', 'playlist_items', 'unresolved_playlist_items', 'playlist_scopes',
             'playlist_group', 'playlist_provenance', 'hidden_playlists', 'accounts'}
SOURCES = {
    'trends': CATALOG | PLAYLISTS | {'play_events', 'play_event_calendar', 'play_event_sources', 'listening_calendars',
        'listening_observations', 'rec_feedback', 'album_tracks', 'album_fetch_state', 'albums', 'album_tracklists',
        'published_album_occurrence_head', 'album_item_observations', 'album_track_occurrences',
        'album_occurrence_sources', 'album_library_memberships', 'account_albums', 'unattributed_album_memberships',
        'album_provider_ids'},
    'cleanup': CATALOG | PLAYLISTS | {'cleanup_ignored', 'suppressed_overlaps',
        'overlap_ignored', 'overlap_kept', 'ignored_merges'},
    'analytics': CATALOG | PLAYLISTS | {'play_events', 'listening_observations', 'rec_feedback',
                                      'listening_rating_actions'},
    'model_health': CATALOG | PLAYLISTS | {'recording_embeddings', 'recording_embedding_files',
        'play_events', 'play_event_calendar', 'play_event_sources', 'listening_observations'},
}
SURFACES = {'trends': 'trend_rollups', 'cleanup': 'cleanup', 'analytics': 'listening_analytics',
            'model_health': 'model_health'}
DEPENDENCIES = {kind: {(table, '') for table in tables} for kind, tables in SOURCES.items()}
for dependencies in DEPENDENCIES.values():
    dependencies.remove(('recordings', ''))
    dependencies.add(('recordings', 'group:catalog'))
DEPENDENCIES['trends'].add(('settings', 'key:takeout_imported_at'))
DEPENDENCIES['analytics'].update(('player_events', 'key:' + kind) for kind in PLAYER_KINDS)
DEPENDENCIES['analytics'].add(('settings', 'key:radio_session_playlist_ids'))
DEPENDENCIES['analytics'].add(('radio_playlist_origins', ''))
DEPENDENCIES['analytics'].add(('settings', 'key:listening_timezone'))
DEPENDENCIES['model_health'].update(('settings', 'key:' + key) for key in (
    'rec_embed_method', 'rec_dim', 'rec_cooc_weighting', 'rec_param:max_track_minutes'))


class SummaryInputs:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings

    def capture(self, kinds):
        kinds = tuple(kinds)
        dependencies = set().union(*(DEPENDENCIES[kind] for kind in kinds))
        surfaces = tuple(SURFACES[kind] for kind in kinds)
        revisions, external, present = self.repository.capture(dependencies, surfaces)
        zone = self.settings.get_setting('listening_timezone') if 'analytics' in kinds else None
        return {kind: (tuple(sorted((table, revision) for (table, key), revision in revisions.items()
                                   if (table, key) in DEPENDENCIES[kind])), external,
                       SURFACES[kind] in present) for kind in kinds}, zone
