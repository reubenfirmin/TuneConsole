"""Enrichment subject and coverage projections; no scheduling policy."""

# One representative input per recording, independent of library/discovery overlap.
TRACKS_CTE = "enrichment_tracks AS (SELECT * FROM catalog_enrichment_tracks)"


def pending_coverage(recording="t.recording_id"):
    return ("EXISTS (SELECT 1 FROM enrichment_provider_jobs j JOIN enrichment_provider_config c "
            "ON c.provider=j.provider WHERE j.recording_id=" + recording + " AND c.status='available')")
