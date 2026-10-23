"""A compact account-day aggregate, separate from the already-applied v037."""


def statements():
    # v037 briefly included this view during development. Both installed v037
    # shapes upgrade to the same definition without rewriting listening data.
    yield 'DROP VIEW IF EXISTS listening_day_counts'
    yield """CREATE VIEW listening_day_counts AS
        SELECT identity_key,COUNT(*) plays FROM (
            SELECT DISTINCT identity_id,identity_key,day FROM eligible_listening_evidence
        ) GROUP BY identity_key"""


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
