"""Immutable serving configuration and explicit recommendation response events (#113)."""


def apply(conn, context):
    conn.execute("""CREATE TABLE rec_serving_policies (
        model_version TEXT PRIMARY KEY, configuration TEXT NOT NULL, created_at REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE rec_response_events (
        id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, kind TEXT NOT NULL,
        identity_key TEXT NOT NULL, at REAL NOT NULL, is_generated INTEGER NOT NULL DEFAULT 0,
        UNIQUE(event_id, kind, identity_key)
    )""")
    conn.execute("CREATE INDEX ix_rec_response_at ON rec_response_events(at)")
