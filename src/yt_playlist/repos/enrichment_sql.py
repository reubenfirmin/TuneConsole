"""Shared enrichment subject and provider eligibility SQL; no connection or transaction owner."""

# One representative input per recording, independent of library/discovery overlap.
TRACKS_CTE = "enrichment_tracks AS (SELECT * FROM catalog_enrichment_tracks)"


def pending_coverage(recording="t.recording_id"):
    return ("EXISTS (SELECT 1 FROM enrichment_provider_jobs j JOIN enrichment_provider_config c "
            "ON c.provider=j.provider WHERE j.recording_id=" + recording + " AND c.status='available')")


def eligible_providers(*, coverage=False):
    # An unfinished pass can run when a pending provider is due, or all have answered
    # (e.g. stopping before completion was stamped). Completed providers do not bypass retries.
    pending = """FROM json_each(:providers) p
        LEFT JOIN enrichment_provider_state s ON s.recording_id=t.recording_id
          AND s.provider=p.value AND s.cycle=COALESCE(t.last_enriched_at,-1)
        LEFT JOIN enrichment_provider_cooldowns c ON c.provider=p.value
        WHERE (s.recording_id IS NULL OR s.status='retry')"""
    if coverage:
        pending += " AND (t.first_enriched_at IS NULL OR EXISTS (SELECT 1 FROM " \
                   "enrichment_provider_jobs j WHERE j.recording_id=t.recording_id AND j.provider=p.value))"
    return ("(:providers IS NULL OR EXISTS (SELECT 1 " + pending +
            " AND COALESCE(s.retry_at,0)<=:now AND COALESCE(c.retry_at,0)<=:now) "
            "OR NOT EXISTS (SELECT 1 " + pending + "))")
