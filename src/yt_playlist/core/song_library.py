"""Small additive owners for exact onboarding picks and native song-sync progress."""
from yt_playlist.core.schema_contract import MigrationError, tokens

DEFINITIONS = {
    'starter_song_choices': '''CREATE TABLE starter_song_choices (
        video_id TEXT PRIMARY KEY NOT NULL,
        selected_at REAL NOT NULL
    )''',
    'song_library_sync': '''CREATE TABLE song_library_sync (
        account_id INTEGER PRIMARY KEY,
        phase TEXT NOT NULL CHECK(phase IN ('complete','partial','failed','skipped')),
        updated_at REAL,
        last_success_at REAL,
        failures INTEGER,
        retry_at REAL
    )''',
}


def validate(conn):
    for name, sql in DEFINITIONS.items():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible native song library owner: ' + name)


def install(conn):
    for name, sql in DEFINITIONS.items():
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (name,)).fetchone() is None:
            conn.execute(sql)
    validate(conn)
