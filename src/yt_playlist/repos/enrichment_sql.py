"""Enrichment subject and coverage projections; no scheduling policy."""
from yt_playlist.core.catalog_query import ENRICHMENT_TRACKS_SQL

# One representative input per recording, independent of library/discovery overlap.
TRACKS_CTE = f"enrichment_tracks AS ({ENRICHMENT_TRACKS_SQL})"


def pending_coverage(recording="t.recording_id"):
    return ("EXISTS (SELECT 1 FROM enrichment_provider_state s JOIN enrichment_provider_config c "
            "ON c.provider=s.provider WHERE s.recording_id=" + recording + " AND s.queued=1 AND c.status='available')")


QUEUE_PROVIDER = ("INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error,queued) ")
QUEUED_VALUES = ",-1,0,'queued',0,0,'',1"
SET_QUEUED = " ON CONFLICT(recording_id,provider) DO UPDATE SET queued=1"
