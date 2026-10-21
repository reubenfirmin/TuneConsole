from .v001_baseline import setting, stamp


def apply(conn, context):
    if setting(conn, 'enrich_unattempted_batch_repaired'):
        return
    conn.execute("UPDATE tracks SET first_enriched_at=NULL,last_enriched_at=NULL WHERE first_enriched_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM enrichment_log e WHERE e.track_id=tracks.id) AND (genre IS NULL OR genre='') AND (mb_year IS NULL OR mb_year='') AND bpm IS NULL AND energy IS NULL AND danceability IS NULL AND mb_recording_id IS NULL")
    stamp(conn, 'enrich_unattempted_batch_repaired')
