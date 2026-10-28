"""Experimental audio observations and explicit labels, separate from track metadata."""


def statements():
    yield ("CREATE TABLE audio_vibe_samples (id TEXT PRIMARY KEY NOT NULL,run_id TEXT NOT NULL,"
           "point TEXT NOT NULL CHECK(point IN ('thirty','middle','two_thirds')),"
           "payload TEXT NOT NULL,created_at REAL NOT NULL,UNIQUE(run_id,point))")
    yield ("CREATE TABLE audio_vibe_feedback (run_id TEXT PRIMARY KEY NOT NULL,"
           "sample_ids TEXT NOT NULL,prediction TEXT NOT NULL,vibe TEXT NOT NULL "
           "CHECK(vibe IN ('','gentle','boost','focus','bright','unwind','move')),updated_at REAL NOT NULL)")


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible audio vibe structure: {name}')
