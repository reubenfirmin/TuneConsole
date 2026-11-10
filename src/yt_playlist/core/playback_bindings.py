"""Bounded exact playback attribution, owned by the account and playlist scope."""
import json

from . import catalog, entity_names, playlist_scopes


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='listening_playback_bindings'").fetchone() is not None


def put(conn,account_id,playlist,video,opportunity,option,now,window_s):
    if not conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?',(video,)).fetchone():
        catalog.upsert(conn,video,'','',preferred_key='@unresolved-video:'+video)
    insert(conn,account_id,playlist,video,opportunity,str(option),now,before=now-window_s,limit=64)


def insert(conn,account_id,playlist,video,opportunity,option,now,*,before,limit):
    scope = playlist_scopes.ensure(conn,playlist,account_id)
    conn.execute('DELETE FROM listening_playback_bindings WHERE bound_at>? OR bound_at<?',(now,before))
    conn.execute('INSERT INTO listening_playback_bindings VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(account_id,playlist_scope_id,video_id) DO UPDATE SET opportunity_id=excluded.opportunity_id,'
        'option_id=excluded.option_id,bound_at=excluded.bound_at',(account_id,scope,video,opportunity,option,now))
    conn.execute('DELETE FROM listening_playback_bindings WHERE (account_id,playlist_scope_id,video_id) IN '
        '(SELECT account_id,playlist_scope_id,video_id FROM listening_playback_bindings '
        'ORDER BY bound_at DESC,account_id,playlist_scope_id,video_id LIMIT -1 OFFSET ?)',(limit,))


def find(conn,account_id,playlist,video,now,window_s):
    return lookup(conn,account_id,playlist,video,before=now-window_s,through=now)


def lookup(conn,account_id,playlist,video,*,before,through):
    scope = playlist_scopes.resolve(conn,playlist,account_id)
    scopes = entity_names.owner(conn, 'playlist_scopes')
    row = conn.execute(f'SELECT b.*,s.{scopes.column("provider_playlist_id")} AS provider_playlist_id '
        'FROM listening_playback_bindings b '
        f'JOIN {scopes.table} s ON s.{scopes.column("id")}=b.playlist_scope_id '
        'WHERE b.account_id=? AND b.playlist_scope_id=? AND (? IS NULL OR b.video_id=?) '
        'AND b.bound_at BETWEEN ? AND ? ORDER BY b.bound_at DESC,b.video_id DESC LIMIT 1',
        (account_id,scope,video,video,before,through)).fetchone()
    return ({'account_id':row['account_id'],'playlist':row['provider_playlist_id'],'video':row['video_id'],
             'opportunity_id':row['opportunity_id'],'option_id':row['option_id'],'at':row['bound_at']} if row else None)


def import_legacy(conn,raw):
    rows = json.loads(raw)
    if not isinstance(rows,list):
        raise ValueError('Legacy playback bindings must be an ordered list')
    for row in rows:
        row = entity_names.account_document(row)
        if (not isinstance(row,dict) or set(row)!={'account_id','playlist','video','opportunity_id','option_id','at'}):
            raise ValueError('Unsupported legacy playback binding; original retained')
        # No age eviction during migration: the old reader applies the caller's
        # window, and no current clock can supply missing account ownership.
        scope = playlist_scopes.ensure(conn,row['playlist'],row['account_id'])
        if not conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?',(row['video'],)).fetchone():
            catalog.upsert(conn,row['video'],'','',preferred_key='@unresolved-video:'+row['video'])
        conn.execute('INSERT INTO listening_playback_bindings VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(account_id,playlist_scope_id,video_id) DO UPDATE SET opportunity_id=excluded.opportunity_id,'
            'option_id=excluded.option_id,bound_at=excluded.bound_at',
            (row['account_id'],scope,row['video'],row['opportunity_id'],str(row['option_id']),row['at']))
