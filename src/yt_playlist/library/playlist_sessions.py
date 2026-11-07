"""Listening rules expressed as projections for bounded SQLite aggregation."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlaylistSessionPolicy:
    gap_seconds: int = 1800
    observations: str = """
        SELECT account_id,played_at at,playlist_ytm_id context FROM play_events
        UNION
        SELECT account_id,at,playlist_ytm_id FROM player_events
        WHERE video_id IS NOT NULL
        AND CASE WHEN json_valid(payload) THEN
            COALESCE(json_extract(payload,'$.muted'),0)=0
            AND COALESCE(json_extract(payload,'$.tabMuted'),0)=0
            AND COALESCE(json_extract(payload,'$.paused'),0)=0
            AND COALESCE(json_extract(payload,'$.volume'),1)>0
            ELSE 1 END
        AND ((kind='tick' AND position>0) OR
             (kind='state' AND CASE WHEN json_valid(payload)
                 THEN json_extract(payload,'$.state') END='playing'))
    """
    context: str = """CASE
        WHEN context LIKE 'RDAMPL%' THEN substr(context,7)
        WHEN context LIKE 'VL%' THEN substr(context,3)
        ELSE context END"""
    starts_session: str = 'previous IS NOT pid OR at-previous_at>:gap_seconds'


PLAYLIST_SESSIONS = PlaylistSessionPolicy()
