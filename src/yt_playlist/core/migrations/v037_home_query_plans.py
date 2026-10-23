"""Keep Home metadata/evidence lookups and serving-ledger retention indexed."""


def statements():
    yield 'CREATE INDEX ix_rec_policy_created ON rec_serving_policies(created_at,model_version)'
    yield 'CREATE INDEX ix_rec_served_model ON rec_served_impressions(model_version)'
    yield ("CREATE INDEX ix_rec_served_pool ON rec_served_impressions"
           "(CAST(json_extract(provenance,'$.pool_policy') AS TEXT))")
    # The former self-lookups through the UNION view built two temporary indexes
    # over the entire evidence ledger. Probe the indexed base tables instead.
    # Keep every evidence column and source contribution, including duplicates.
    yield 'DROP VIEW eligible_listening_evidence'
    yield """CREATE VIEW eligible_listening_evidence AS
        SELECT CASE WHEN o.capture_item_id IS NOT NULL THEN 'capture:'||o.capture_item_id
                    ELSE 'day:'||o.id END evidence_id,
               o.identity_id,o.identity_key,o.recording_id,o.day,o.source,o.timezone,
               o.precision,o.last_observed_at observed_at,NULL event_id,0 is_generated
        FROM listening_observations o WHERE o.day IS NOT NULL AND (
            o.source<>'ytm_history' OR NOT EXISTS (
                SELECT 1 FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
                JOIN play_event_sources s ON s.event_id=pe.id
                WHERE pe.identity_id=o.identity_id AND pe.identity_key=o.identity_key
                  AND c.day=o.day AND pe.is_generated=1)
            OR EXISTS (
                SELECT 1 FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
                JOIN play_event_sources s ON s.event_id=pe.id
                WHERE pe.identity_id=o.identity_id AND pe.identity_key=o.identity_key
                  AND c.day=o.day AND COALESCE(pe.is_generated,0)=0)
            OR EXISTS (
                SELECT 1 FROM listening_observations independent
                WHERE independent.identity_id=o.identity_id AND independent.identity_key=o.identity_key
                  AND independent.day=o.day AND independent.source<>'ytm_history'))
        UNION ALL
        SELECT 'event:'||pe.id||':'||s.source,pe.identity_id,pe.identity_key,pe.recording_id,c.day,
               s.source,c.timezone,'exact_timestamp',pe.played_at,pe.id,COALESCE(pe.is_generated,0)
        FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id
        JOIN play_event_sources s ON s.event_id=pe.id WHERE COALESCE(pe.is_generated,0)=0"""


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
