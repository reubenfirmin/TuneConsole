"""Complete the source projection and lookup indexes after applied migration 34.

Some development databases applied the additions before they had their own
version. Preserve those objects and validate their definitions at version 35.
"""

OBJECTS = (
    ('ix_rec_playlist_scope_cursor',
     'CREATE INDEX ix_rec_playlist_scope_cursor ON playlists(scope_ref,id)'),
    ('ix_rec_album_cursor',
     'CREATE INDEX ix_rec_album_cursor ON album_tracks(album_id,id)'),
    ('rec_source_members',
     'CREATE TABLE rec_source_members (source TEXT NOT NULL,identity_key TEXT NOT NULL,weight REAL NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(source,identity_key))'),
    ('ix_rec_source_members_key',
     'CREATE INDEX ix_rec_source_members_key ON rec_source_members(identity_key,updated_at DESC,source)'),
    ('ix_rec_candidates_key',
     'CREATE INDEX ix_rec_candidates_key ON rec_candidate_rows(identity_key)'),
    ('ix_rec_rank_score',
     'CREATE INDEX ix_rec_rank_score ON rec_rank_residual(mode_id,score DESC,identity_key)'),
    ('ix_rec_rank_key',
     'CREATE INDEX ix_rec_rank_key ON rec_rank_residual(identity_key,mode_id)'),
    ('ix_rec_forecast_pending',
     'CREATE INDEX ix_rec_forecast_pending ON rec_local_forecasts(issued_at,identity_key) WHERE outcome IS NULL'),
)


def statements():
    yield from (sql for _, sql in OBJECTS)


def apply(conn, context):
    missing_members = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='rec_source_members'").fetchone() is None
    for name, sql in OBJECTS:
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone() is None:
            conn.execute(sql)
    if missing_members:
        # Recover this derived projection from the exact contribution ledger;
        # keep existing vectors, optimizer state, checkpoints and source rows.
        conn.execute('INSERT INTO rec_source_members(source,identity_key,weight,updated_at) '
                     'SELECT source,identity_key,SUM(weight),0 FROM rec_evidence '
                     'GROUP BY source,identity_key HAVING SUM(weight)>0')
        conn.execute("INSERT INTO rec_change_queue(kind,item) "
                     "SELECT 'node',identity_key FROM rec_source_members GROUP BY identity_key "
                     'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1')
