"""Collection kinds and their stable archive owner names."""
from .taste_settings import BASELINE, CHECKPOINT, STATE_KEYS


SYNC_ATTEMPT = 'library_sync_attempt'
TASTE_CONTROLS = {
    'taste_settings_checkpoints': STATE_KEYS[CHECKPOINT],
    'taste_comparison_baselines': STATE_KEYS[BASELINE],
}
OWNERS = {'road_trip_recipes':'road-trip-draft','radio_sessions':'radio-session',
          'sync_cursors':'library-sync','taste_model_state':'taste-settings',
          'taste_settings_checkpoints':'taste-checkpoint','taste_comparison_baselines':'taste-comparison',
          'experiment_decisions':'experiment-decision','spotify_import_jobs':'spotify-import',
          'rec_recipes':'playlist-recipe','cluster_canvas':'playlist-canvas',
          'actions':'action','action_steps':'action-step',
          'spotify_transfer_receipts':'spotify-transfer',
          'tv_append_intents':'tv-append','discovered_artists':'discovery-candidate'}
OWNER_KEYS = {'road_trip_recipes':'draft_id','discovered_artists':'artist_id','sync_cursors':'recovery_owner'}
PLAYLIST_DOCUMENTS = {'rec_recipes': 'recipe', 'cluster_canvas': 'canvas'}
PREDECESSOR_CLEANUP = 'playlist_gc_attempts', 'playlist-gc'
PREDECESSOR_OWNERS = {
    'road_trip_drafts': 'road-trip-draft',
    'action_drafts': 'action-draft',
    'sync_attempts': 'library-sync',
}
