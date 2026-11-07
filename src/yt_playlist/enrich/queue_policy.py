"""Scheduling criteria compiled by the enrichment queue's SQLite queries."""
from yt_playlist.core import provider_queries

ADMISSION_BATCH = 64
VISIBLE_TRACKS = 96


# New arrivals, direct listening, engaged containers, then the remaining backlog.
PRIORITY_ORDER = """
    CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN 0
         WHEN sp > 0 THEN 1
         WHEN best > 0 THEN 2 ELSE 3 END ASC,
    CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN -created_at
         WHEN sp > 0 THEN -sp ELSE -best END ASC,
    created_at DESC, queue_order ASC, source ASC
"""


def primary_eligibility(pending_coverage):
    return "(t.first_enriched_at IS NULL OR " + pending_coverage + ") AND " + eligible_providers(coverage=True)


def priority_eligibility(pending_coverage):
    return ("(genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='') "
            "AND (first_enriched_at IS NULL OR last_enriched_at<:stale_before OR " +
            pending_coverage + ") AND " + eligible_providers())


def resweep_eligibility(pending_coverage):
    return ("t.first_enriched_at IS NOT NULL AND t.last_enriched_at < :stale_before "
            "AND (t.genre IS NULL OR t.genre='' OR t.mb_year IS NULL OR t.mb_year='' "
            "OR t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL) "
            "AND NOT " + pending_coverage + " AND " + eligible_providers())


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
    input_ready = "(p.value<>'lastfm' OR (" + provider_queries.lastfm_ready_sql() + "))"
    return ("(:providers IS NULL OR EXISTS (SELECT 1 " + pending +
            " AND " + input_ready +
            " AND COALESCE(s.retry_at,0)<=:now AND COALESCE(c.retry_at,0)<=:now) "
            "OR (NOT EXISTS (SELECT 1 " + pending + ") AND EXISTS "
            "(SELECT 1 FROM json_each(:providers) p WHERE " + input_ready + ")))")
