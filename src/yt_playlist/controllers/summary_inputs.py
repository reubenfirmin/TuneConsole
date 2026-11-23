"""Summary dependencies and configuration composed from database facts."""
from yt_playlist.core.playlist_scope_state import MERGE_DISMISSALS
from yt_playlist.core.rating_actions import KIND as CONFIRMED_RATING
from yt_playlist.core.radio_provenance import UNRESOLVED
from yt_playlist.rec.listening_policy import PLAYER_KINDS

CATALOG = {'recordings', 'youtube_items', 'track_sources', 'library_memberships',
           'entry_field_choices',
           'entry_artists', 'track_artists', 'artists'}
PLAYLISTS = {'playlists', 'playlist_items', 'playlist_scopes',
             'playlist_group', 'playlist_provenance', 'hidden_playlists', 'accounts'}
SOURCES = {
    'trends': CATALOG | PLAYLISTS | {'play_events', 'play_event_calendar', 'play_event_sources', 'accounts',
        'listening_observations', 'rec_feedback', 'album_tracks', 'albums', 'album_tracklists',
        'published_album_occurrence_head', 'album_item_observations', 'album_track_occurrences',
        'album_occurrence_sources', 'album_library_memberships', 'account_albums',
        'album_provider_ids'},
    'cleanup': CATALOG | PLAYLISTS | {'playlist_overlap_decisions'},
    'analytics': CATALOG | PLAYLISTS | {'play_events', 'listening_observations', 'rec_feedback'},
    'model_health': CATALOG | PLAYLISTS | {'recording_embedding_files',
        'play_events', 'play_event_calendar', 'play_event_sources', 'listening_observations'},
    'library_taste': CATALOG | {'play_events', 'play_event_calendar', 'play_event_sources', 'listening_observations',
        'metadata_observations', 'genre_family', 'genre_catalog', 'genre_alias',
        'recommendation_generation_surfaces'},
}
SURFACES = {'trends': 'trend_rollups', 'cleanup': 'cleanup', 'analytics': 'listening_analytics',
            'model_health': 'model_health', 'library_taste': 'library_taste_summary'}
DEPENDENCIES = {kind: {(table, '') for table in tables} for kind, tables in SOURCES.items()}
for dependencies in DEPENDENCIES.values():
    dependencies.remove(('recordings', ''))
    dependencies.add(('recordings', 'group:catalog'))
    dependencies.remove(('artists', ''))
    dependencies.add(('artists', 'group:identity'))
for table in ('playlists', 'playlist_scopes'):
    DEPENDENCIES['trends'].remove((table, ''))
    DEPENDENCIES['trends'].add((table, 'group:recap'))
DEPENDENCIES['trends'].add(('settings', 'key:takeout_imported_at'))
DEPENDENCIES['cleanup'].add(('settings', 'key:' + MERGE_DISMISSALS))
DEPENDENCIES['analytics'].update(('player_events', 'key:' + kind) for kind in (*PLAYER_KINDS, CONFIRMED_RATING))
DEPENDENCIES['analytics'].add(('settings', 'key:' + UNRESOLVED))
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
