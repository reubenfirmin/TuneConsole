"""Index unresolved calendar scopes without restoring mutable canonical keys."""


def statements():
    yield ("CREATE INDEX ix_listening_observation_unresolved_scope ON listening_observations "
           "(account_id,unresolved_subject_key,source_day,timezone) "
           "WHERE recording_id IS NULL AND precision='calendar_day'")
    yield ("CREATE INDEX ix_play_event_account_unresolved_time ON play_events "
           "(account_id,unresolved_subject_key,played_at) WHERE unresolved_subject_key IS NOT NULL")


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
