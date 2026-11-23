"""Original layout for initial TV launches."""


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
    from ..schema_contract import MigrationError, tokens
    for name, sql in definitions(conn).items():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible initial TV launch owner: ' + name)
