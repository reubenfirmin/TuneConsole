"""Preserve confirmed rating transitions for descriptive listening history."""


def statements():
    yield ("CREATE TABLE listening_rating_actions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
           "identity_id INTEGER NOT NULL REFERENCES identities(id),video_id TEXT NOT NULL,"
           "previous_status TEXT,status TEXT NOT NULL,source TEXT NOT NULL,at REAL NOT NULL)")
    yield 'CREATE INDEX ix_listening_rating_time ON listening_rating_actions(at,id)'


def apply(conn, now):
    for sql in statements():
        conn.execute(sql)
