"""Keep playlist deletion admission independent of completed action history."""


def apply(conn, context):
    conn.execute("CREATE INDEX ix_actions_delete_pending ON actions(id DESC) "
                 "WHERE status='delete_pending'")
