"""The pending deliberate album queue and its exact uploaded occurrences."""
import json

from . import albums, catalog, collection_recovery, entity_names, playlist_scopes

KEY = 'listening_album_pending'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='album_playback_intents'").fetchone() is not None


@collection_recovery.pinned_read
def load(conn):
    if not available(conn):
        from .published_documents import PENDING_ALBUM as owner
        revision = owner.selected(conn)
        if revision is not None:
            return entity_names.account_document(json.loads(owner.value(conn,revision) or 'null'))
        row = conn.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
        return entity_names.account_document(json.loads(row[0])) if row and row[0] else None
    scopes = entity_names.owner(conn, 'playlist_scopes')
    rows = list(conn.execute(f'SELECT i.*,s.{scopes.column("provider_playlist_id")} AS provider_playlist_id,q.position,q.video_id '
        'FROM album_playback_intents i '
        f'JOIN {scopes.table} s ON s.{scopes.column("id")}=i.playlist_scope_id '
        'LEFT JOIN album_playback_intent_items q ON q.intent_id=i.singleton '
        'WHERE i.singleton=1 ORDER BY q.position'))
    if not rows:
        return None
    row = rows[0]
    result = {'opportunity_id':row['opportunity_id'],'browse':row['source_browse_id'],
              'playlist':row['provider_playlist_id'],'at':row['started_at']}
    if row['action_id'] is not None:
        result['action_id'] = row['action_id']
    if row['account_id'] is not None:
        result['account_id'] = row['account_id']
    if row['has_videos']:
        result['videos'] = [item['video_id'] for item in rows if item['position'] is not None]
    return result


def put(conn,value):
    value = entity_names.account_document(value)
    if not available(conn):
        conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (KEY,json.dumps(entity_names.external_account_document(value))))
        return
    if value is None:
        conn.execute('DELETE FROM album_playback_intents WHERE singleton=1')
        return
    if (not isinstance(value,dict) or set(value)-{'opportunity_id','browse','playlist','at','action_id','videos','account_id'}
            or not {'opportunity_id','browse','playlist','at'}<=value.keys()
            or ('videos' in value and not isinstance(value['videos'],list))):
        raise ValueError('Unsupported pending album playback context')
    conn.execute('UPDATE album_playback_intents SET revision=revision WHERE 0')
    album = albums.observe(conn,value['browse'])
    scope = playlist_scopes.ensure(conn,value['playlist'],value.get('account_id'))
    conn.execute('INSERT INTO album_playback_intents(singleton,account_id,album_id,source_browse_id,playlist_scope_id,'
        'opportunity_id,action_id,started_at,has_videos,revision) VALUES (1,?,?,?,?,?,?,?,?,1) '
        'ON CONFLICT(singleton) DO UPDATE SET account_id=excluded.account_id,album_id=excluded.album_id,'
        'source_browse_id=excluded.source_browse_id,playlist_scope_id=excluded.playlist_scope_id,'
        'opportunity_id=excluded.opportunity_id,action_id=excluded.action_id,started_at=excluded.started_at,'
        'has_videos=excluded.has_videos,revision=revision+1',
        (value.get('account_id'),album,value['browse'],scope,value['opportunity_id'],value.get('action_id'),
         value['at'],int('videos' in value)))
    conn.execute('DELETE FROM album_playback_intent_items WHERE intent_id=1')
    for position,video in enumerate(value.get('videos',[])):
        if not isinstance(video,str) or not video:
            raise ValueError('Pending album playback requires its exact video IDs')
        if not conn.execute('SELECT 1 FROM youtube_items WHERE video_id=?',(video,)).fetchone():
            catalog.upsert(conn,video,'','',preferred_key='@unresolved-video:'+video)
        conn.execute('INSERT INTO album_playback_intent_items VALUES (1,?,?)',(position,video))


def bind(conn,pending,account_id):
    if not available(conn):
        put(conn,{**pending,'account_id':account_id})
        return
    conn.execute('UPDATE album_playback_intents SET account_id=?,revision=revision+1 WHERE singleton=1 '
        'AND account_id IS NULL AND opportunity_id=? AND started_at=? AND action_id IS ?',
        (account_id,pending['opportunity_id'],pending['at'],pending.get('action_id')))
