"""Durable initial TV intents, independent of disposable recommendation output."""

FIELDS = ('title', 'artist', 'thumbnail', 'duration', 'kind', 'source_key',
          'source_artist', 'source_group', 'connection')


def definitions(conn):
    account = 'accounts' if conn.execute(
        "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='accounts'").fetchone() else 'identities'
    return {
        'tv_launches': f'''CREATE TABLE tv_launches (
            request_id TEXT PRIMARY KEY NOT NULL CHECK(length(request_id) BETWEEN 1 AND 64),
            account_id INTEGER REFERENCES {account}(id) ON DELETE SET NULL,
            owner_id INTEGER NOT NULL,
            stream TEXT NOT NULL CHECK(length(stream)=32),
            max_minutes INTEGER CHECK(max_minutes IS NULL OR max_minutes>0),
            provider_playlist_id TEXT CHECK(provider_playlist_id IS NULL OR length(provider_playlist_id)>0),
            title TEXT NOT NULL DEFAULT 'TV',
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','complete')),
            current INTEGER NOT NULL DEFAULT 1 CHECK(current IN (0,1)),
            created_at REAL NOT NULL,
            CHECK(account_id IS NULL OR account_id=owner_id),
            CHECK(state='pending' OR provider_playlist_id IS NOT NULL))''',
        'ix_tv_launch_current': 'CREATE INDEX ix_tv_launch_current ON tv_launches(owner_id,current,state)',
        'tv_launch_videos': '''CREATE TABLE tv_launch_videos (
            request_id TEXT NOT NULL REFERENCES tv_launches(request_id),
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 59),
            video_id TEXT NOT NULL,
            title TEXT, artist TEXT, thumbnail TEXT, duration INTEGER, kind TEXT,
            source_key TEXT, source_artist TEXT, source_group TEXT, connection TEXT,
            write_state TEXT NOT NULL DEFAULT 'uncertain'
                CHECK(write_state IN ('confirmed','uncertain','rejected','unattempted')),
            PRIMARY KEY(request_id,position), UNIQUE(request_id,video_id)) WITHOUT ROWID''',
    }


def install(conn, context=None):
    for name, sql in definitions(conn).items():
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (name,)).fetchone():
            conn.execute(sql)
    validate(conn)


def validate(conn):
    from .schema_contract import MigrationError, tokens
    for name, sql in definitions(conn).items():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible initial TV launch owner: ' + name)


def read(conn, request_id):
    row = conn.execute('SELECT * FROM tv_launches WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['items'] = []
    result['outcome'] = {state: [] for state in ('confirmed', 'uncertain', 'rejected', 'unattempted')}
    for item in conn.execute('SELECT * FROM tv_launch_videos WHERE request_id=? ORDER BY position', (request_id,)):
        track = {field: item[field] for field in ('video_id', *FIELDS) if item[field] is not None}
        if 'source_group' in track:
            track['group'] = track.pop('source_group')
        result['items'].append(track)
        result['outcome'][item['write_state']].append(item['video_id'])
    return result


def begin(conn, request_id, account_id, stream, maximum, items, now):
    """The caller reserves a writer before checking and capturing the first intent."""
    if conn.execute('SELECT 1 FROM tv_launches WHERE request_id=?', (request_id,)).fetchone():
        return False
    if not items or len(items) > 60 or len({t['video_id'] for t in items}) != len(items):
        raise ValueError('An initial TV launch needs a bounded, distinct selection')
    if conn.execute("SELECT count(*) FROM tv_launches WHERE state='pending'").fetchone()[0] >= 60:
        raise ValueError('Too many unresolved TV launches')
    conn.execute('UPDATE tv_launches SET current=0 WHERE owner_id=? AND current=1', (account_id,))
    conn.execute('INSERT INTO tv_launches(request_id,account_id,owner_id,stream,max_minutes,created_at) '
                 'VALUES (?,?,?,?,?,?)', (request_id, account_id, account_id, stream, maximum, now))
    conn.executemany('INSERT INTO tv_launch_videos(request_id,position,video_id,' + ','.join(FIELDS) + ') '
                     'VALUES (' + ','.join('?' for _ in range(3 + len(FIELDS))) + ')', [
        (request_id, index, item['video_id'], *(item.get('group' if field == 'source_group' else field)
                                              for field in FIELDS)) for index, item in enumerate(items)])
    return True


def destination(conn, request_id, remote_id, title):
    row = conn.execute('SELECT provider_playlist_id FROM tv_launches WHERE request_id=?', (request_id,)).fetchone()
    if row is None or row[0] not in (None, remote_id):
        raise ValueError('The initial TV destination belongs to another launch')
    conn.execute('UPDATE tv_launches SET provider_playlist_id=?,title=? WHERE request_id=?',
                 (remote_id, title, request_id))


def outcome(conn, request_id, values):
    for state, videos in values.items():
        conn.executemany('UPDATE tv_launch_videos SET write_state=? WHERE request_id=? AND video_id=?',
                         [(state, request_id, video) for video in videos])


def complete(conn, request_id):
    conn.execute("UPDATE tv_launches SET state='complete' WHERE request_id=?", (request_id,))
    conn.execute("UPDATE tv_launch_videos SET write_state='confirmed' WHERE request_id=? AND write_state='uncertain'",
                 (request_id,))
