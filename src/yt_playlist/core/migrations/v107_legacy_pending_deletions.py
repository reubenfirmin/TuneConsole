"""Require readback for pending deletion attempts imported before v98."""


def apply(conn, context):
    # v98 records the original action ID in this reserved import key. Current
    # unsent intents have their own admission checkpoint and remain not_sent.
    # Retain every captured document and backup byte; the relational phase is
    # overlaid by the normal action reader, including after restart.
    conn.execute("UPDATE action_steps SET phase='uncertain',outcome='unknown' "
                 "WHERE operation='delete_playlist' AND phase='pending' AND outcome='not_sent' "
                 "AND action_id IN (SELECT id FROM actions WHERE idempotency_key='legacy-action:'||id)")
