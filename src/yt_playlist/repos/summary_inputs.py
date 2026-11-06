"""Revision and publication facts used by summary maintenance."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.listening_analytics import PLAYER_KINDS

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
DEPENDENCIES['trends'].add(('settings', 'key:takeout_imported_at'))
DEPENDENCIES['analytics'].update(('player_events', 'key:' + kind) for kind in PLAYER_KINDS)
DEPENDENCIES['analytics'].add(('settings', 'key:radio_session_playlist_ids'))
DEPENDENCIES['analytics'].add(('radio_playlist_origins', ''))
DEPENDENCIES['analytics'].add(('settings', 'key:listening_timezone'))
DEPENDENCIES['model_health'].update(('settings', 'key:' + key) for key in (
    'rec_embed_method', 'rec_dim', 'rec_cooc_weighting', 'rec_param:max_track_minutes'))


class SummaryInputsRepo(Repo):
    @synchronized
    def capture(self, kinds):
        dependencies = set().union(*(DEPENDENCIES[kind] for kind in kinds))
        revisions = self._store._snapshot_revisions.capture(dependencies)
        # External import commits must invalidate summaries as well as tracked
        # changes made through the serving connection.
        external = self.conn.execute('PRAGMA data_version').fetchone()[0]
        surfaces = tuple(SURFACES[kind] for kind in kinds)
        present = {row[0] for row in self.conn.execute(
            'SELECT surface FROM rec_proposals WHERE surface IN (' + ','.join('?' for _ in surfaces) + ')', surfaces)}
        if self._store._home_display_files:
            present.update(row[0] for row in self.conn.execute('SELECT surface FROM home_display_heads'))
        if self._store._recommendation_reports:
            present.update(row[0] for row in self.conn.execute('SELECT surface FROM recommendation_report_heads'))
        if self._store._listening_files and self.conn.execute('SELECT 1 FROM listening_output_heads h '
                'JOIN listening_output_files f ON f.publication_id=h.publication_id '
                "WHERE h.slot='current' AND f.surface='listening_analytics'").fetchone():
            present.add('listening_analytics')
        zone = self._store.settings.get_setting('listening_timezone') if 'analytics' in kinds else None
        return {kind: (tuple(sorted((table, revision) for (table, key), revision in revisions.items()
                                   if (table, key) in DEPENDENCIES[kind])), external,
                       SURFACES[kind] in present) for kind in kinds}, zone
