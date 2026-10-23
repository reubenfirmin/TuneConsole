"""Index bounded report attribution without changing historical serving evidence."""


def statements():
    yield """CREATE INDEX ix_rec_served_video_at ON rec_served_impressions(
        json_extract(provenance,'$.candidate.video_id'),served_at,id)
        WHERE COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live'"""
    yield 'CREATE INDEX ix_play_events_key_time ON play_events(identity_key,played_at)'
    yield 'CREATE INDEX ix_rec_response_key_time ON rec_response_events(identity_key,at)'


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
