"""Keep listening-session judgments separate from earlier sound-target labels."""


def statements():
    yield ("CREATE TABLE audio_session_labels (run_id TEXT NOT NULL,taxonomy_version TEXT NOT NULL,"
           "sample_ids TEXT NOT NULL,prediction TEXT NOT NULL,vibes TEXT NOT NULL,"
           "updated_at REAL NOT NULL,PRIMARY KEY(run_id,taxonomy_version))")


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
            raise MigrationError(f'Incompatible audio session label structure: {name}')
