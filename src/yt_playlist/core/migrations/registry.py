from .runner import Migration
from . import (v001_baseline, v002_enrichment, v003_discovery_audit, v004_playlist_dates,
               v005_retired_lanes, v006_history_window, v007_like_ratchet, v008_recordings,
               v009_served_outcomes, v010_play_event_subjects)

MIGRATIONS = (
    Migration(1, 'legacy_baseline', v001_baseline.apply),
    Migration(2, 'enrichment_attempt_repair', v002_enrichment.apply),
    Migration(3, 'discovery_audit_requeue', v003_discovery_audit.apply),
    Migration(4, 'playlist_creation_dates', v004_playlist_dates.apply),
    Migration(5, 'retired_source_lanes', v005_retired_lanes.apply),
    Migration(6, 'retire_history_window', v006_history_window.apply),
    Migration(7, 'like_ratchet_repair', v007_like_ratchet.apply),
    Migration(8, 'recordings_and_youtube_items', v008_recordings.apply),
    Migration(9, 'served_policy_and_outcomes', v009_served_outcomes.apply, replaces_data=False),
    Migration(10, 'play_event_subjects', v010_play_event_subjects.apply),
)
