"""Durable local learning jobs and retractable source contributions.

Capture belongs to the source transaction, including imports which do not run in
the web process. No existing model is discarded and no corpus is copied here.
The worker bootstraps older evidence with persisted keyset cursors.
"""

TABLES = (
    "CREATE TABLE rec_change_queue (kind TEXT NOT NULL,item TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(kind,item))",
    "CREATE TABLE rec_learning_state (key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL)",
    "CREATE TABLE rec_evidence (kind TEXT NOT NULL,item TEXT NOT NULL,source TEXT NOT NULL,identity_key TEXT NOT NULL,weight REAL NOT NULL,PRIMARY KEY(kind,item,source,identity_key))",
    "CREATE INDEX ix_rec_evidence_source_key ON rec_evidence(source,identity_key)",
    "CREATE INDEX ix_rec_evidence_key_source ON rec_evidence(identity_key,source)",
    "CREATE TABLE rec_local_nodes (identity_key TEXT PRIMARY KEY NOT NULL,anchor BLOB NOT NULL,optimizer BLOB NOT NULL,steps INTEGER NOT NULL,graph_digest TEXT NOT NULL,updated_at REAL NOT NULL)",
    "CREATE TABLE rec_local_edges (identity_key TEXT NOT NULL,neighbor TEXT NOT NULL,weight REAL NOT NULL,PRIMARY KEY(identity_key,neighbor))",
    "CREATE INDEX ix_rec_local_edges_neighbor ON rec_local_edges(neighbor,identity_key)",
    "CREATE TABLE rec_cluster_members (identity_key TEXT PRIMARY KEY NOT NULL,mode_id INTEGER NOT NULL,vector BLOB NOT NULL,family TEXT NOT NULL)",
    "CREATE INDEX ix_rec_cluster_members_mode ON rec_cluster_members(mode_id,identity_key)",
    "CREATE TABLE rec_cluster_stats (mode_id INTEGER PRIMARY KEY,total BLOB NOT NULL,n INTEGER NOT NULL,families TEXT NOT NULL)",
    "CREATE TABLE rec_candidate_rows (mode_id INTEGER NOT NULL,surface TEXT NOT NULL,identity_key TEXT NOT NULL,payload TEXT NOT NULL,score REAL NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(mode_id,surface,identity_key))",
    "CREATE INDEX ix_rec_candidates_rank ON rec_candidate_rows(mode_id,surface,score DESC,identity_key)",
    "CREATE TABLE rec_rank_residual (mode_id INTEGER NOT NULL,identity_key TEXT NOT NULL,residual REAL NOT NULL,score REAL NOT NULL,PRIMARY KEY(mode_id,identity_key))",
    "CREATE TABLE rec_artist_contributions (identity_key TEXT NOT NULL,artist TEXT NOT NULL,collab BLOB,content BLOB,PRIMARY KEY(identity_key,artist))",
    "CREATE INDEX ix_rec_artist_contributions_artist ON rec_artist_contributions(artist,identity_key)",
    "CREATE TABLE rec_artist_stats (artist TEXT PRIMARY KEY NOT NULL,collab BLOB,content BLOB,n INTEGER NOT NULL)",
    "CREATE TABLE rec_projection_rows (identity_key TEXT PRIMARY KEY NOT NULL,x BLOB NOT NULL,y BLOB NOT NULL)",
    "CREATE TABLE rec_projection_state (id INTEGER PRIMARY KEY CHECK(id=1),xx BLOB NOT NULL,xy BLOB NOT NULL,weights BLOB NOT NULL,n INTEGER NOT NULL,content_dim INTEGER NOT NULL,latent_dim INTEGER NOT NULL)",
    "CREATE TABLE rec_challenger_nodes (identity_key TEXT PRIMARY KEY NOT NULL,vec BLOB NOT NULL,optimizer BLOB NOT NULL,steps INTEGER NOT NULL)",
    "CREATE TABLE rec_local_forecasts (identity_key TEXT PRIMARY KEY NOT NULL,issued_at REAL NOT NULL,incumbent TEXT NOT NULL,challenger TEXT NOT NULL,outcome TEXT)",
)

# A trigger only enqueues a primary key. It never fans out a large playlist or
# performs learning on the writer. UPDATE compares actual values, so unchanged
# sync/import rows do not create fresh work.
SOURCES = (
    ('recordings', 'track', 'id', ('identity_key', 'genre', 'bpm', 'energy', 'danceability', 'music_key',
        'music_scale', 'mood_happy', 'mood_sad', 'mood_relaxed', 'mood_acoustic', 'instrumental')),
    ('youtube_items', 'track', 'recording_id', ('recording_id', 'available', 'title', 'artist', 'album',
        'mb_year', 'duration_s', 'video_type', 'loudness', 'dynamic_complexity')),
    ('catalog_entries', 'entry', 'id', ('recording_id', 'video_id')),
    ('library_memberships', 'entry', 'entry_id', ('entry_id',)),
    ('entry_field_choices', 'entry', 'entry_id', ('value_json',)),
    ('playlist_items', 'playlist', 'json_array(playlist_id,video_id)', ('playlist_id', 'video_id')),
    ('unresolved_playlist_items', 'unresolved_playlist', 'json_array(playlist_id,entry_id)', ('playlist_id', 'entry_id')),
    ('track_artists', 'artist', 'json_array(entry_id,position)', ('artist_id', 'credit')),
    ('album_tracks', 'album', 'id', ('album_id', 'entry_id', 'recording_id')),
    ('album_library_memberships', 'album_scan', 'album_id', ('album_id',)),
    ('play_events', 'event', 'id', ('identity_id', 'identity_key', 'recording_id', 'played_at', 'is_generated')),
    ('play_event_calendar', 'event', 'event_id', ('day', 'timezone')),
    ('listening_observations', 'day', 'id', ('identity_id', 'identity_key', 'recording_id', 'day', 'source', 'precision')),
    ('rec_feedback', 'feedback', 'json_array(surface,item_key,scope)', ('kind', 'until', 'recording_id')),
    ('discovery_memberships', 'track', 'recording_id', ('entry_id',)),
    ('playlist_provenance', 'scope', 'ytm', ('origin', 'adopted')),
    ('playlist_group', 'scope', 'ytm', ('name',)),
    ('playlists', 'playlist_scan', 'id', ('scope_ref',)),
)


def item_expression(expression, prefix):
    if expression.startswith('json_array('):
        columns = expression[len('json_array('):-1].split(',')
        return 'json_array(' + ','.join(f'{prefix}.{c}' for c in columns) + ')'
    return f'CAST({prefix}.{expression} AS TEXT)'


def statements():
    yield from TABLES
    for table, kind, expression, columns in SOURCES:
        for event, prefix in (('INSERT', 'NEW'), ('DELETE', 'OLD'), ('UPDATE', 'NEW')):
            item = item_expression(expression, prefix)
            when = ''
            if event == 'UPDATE':
                when = ' WHEN ' + ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
            # Preserve the old key too when the source's primary identity changes.
            old = ''
            if event == 'UPDATE':
                old_item = item_expression(expression, 'OLD')
                old = (f"INSERT INTO rec_change_queue(kind,item) VALUES ('{kind}',{old_item}) "
                       'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1;')
            yield (f'CREATE TRIGGER rec_capture_{table}_{event.lower()} AFTER {event} ON {table}{when} BEGIN '
                   f"INSERT INTO rec_change_queue(kind,item) VALUES ('{kind}',{item}) "
                   f'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1;{old} END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
