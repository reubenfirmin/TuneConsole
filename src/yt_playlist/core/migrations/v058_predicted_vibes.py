"""Disposable vibe evidence, sufficient statistics and resumable dependency work."""


def statements():
    yield ('CREATE TABLE rec_vibe_tracks (identity_key TEXT PRIMARY KEY NOT NULL,'
           'artist TEXT,genre TEXT,evidence TEXT,version TEXT NOT NULL)')
    yield 'CREATE INDEX idx_vibe_artist_genre ON rec_vibe_tracks(artist,genre,identity_key)'
    yield 'CREATE INDEX idx_vibe_artist ON rec_vibe_tracks(artist,identity_key)'
    yield 'CREATE INDEX idx_vibe_genre ON rec_vibe_tracks(genre,identity_key)'
    yield ('CREATE TABLE rec_vibe_groups (group_key TEXT PRIMARY KEY NOT NULL,stats TEXT NOT NULL)')
    yield ('CREATE TABLE rec_vibe_pending (position INTEGER PRIMARY KEY AUTOINCREMENT,'
           "group_key TEXT UNIQUE NOT NULL,revision INTEGER NOT NULL DEFAULT 1,cursor TEXT NOT NULL DEFAULT '')")
    yield ('CREATE INDEX idx_audio_vibe_video ON audio_vibe_samples('
           "json_extract(payload,'$.video_id'),created_at DESC,id)")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        sides = ('OLD', 'NEW') if operation == 'UPDATE' else (('OLD',) if operation == 'DELETE' else ('NEW',))
        writes = ''.join(
            'INSERT INTO rec_content_pending(identity_key) SELECT r.identity_key FROM youtube_items y '
            'JOIN catalog_entries e ON e.video_id=y.video_id JOIN recordings r ON r.id=e.recording_id '
            f"WHERE y.video_id=json_extract({side}.payload,'$.video_id') "
            'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;' for side in sides)
        yield f'CREATE TRIGGER vibe_samples_{operation.lower()} AFTER {operation} ON audio_vibe_samples BEGIN {writes} END'
        writes = ''.join(
            'INSERT INTO rec_content_pending(identity_key) SELECT r.identity_key FROM catalog_entries e '
            f'JOIN recordings r ON r.id=e.recording_id WHERE e.id={side}.entry_id '
            'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;' for side in sides)
        yield f'CREATE TRIGGER vibe_artists_{operation.lower()} AFTER {operation} ON track_artists BEGIN {writes} END'


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
    # The worker walks this job in pages. Startup never scans the user's corpus.
    conn.execute("INSERT INTO rec_vibe_pending(group_key) VALUES ('*')")
    # Small retained measurement history gets first useful publication ahead of
    # the catalog walk. No prediction or human annotation is copied as evidence.
    conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT DISTINCT r.identity_key '
                 "FROM audio_vibe_samples s JOIN youtube_items y ON y.video_id=json_extract(s.payload,'$.video_id') "
                 'JOIN recordings r ON r.id=y.recording_id WHERE true '
                 'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1')


def validate(conn):
    from .current import _tokens
    from .runner import MigrationError
    for sql in statements():
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError(f'Incompatible predicted vibe structure: {name}')
