"""Typed library progress with immutable attempt details for restart and display."""
import json

from . import collection_recovery, entity_names, playlist_scopes

KEY = 'library_sync_state'
STATUSES = frozenset({'complete','partial','failed','skipped'})
RESOURCES = frozenset({'playlist_listing','playlists','ratings','history','saved_albums','saved_album_mirror','album_tracks'})
STAMPS = {'last_sync_at':'last_success_at','last_sync_attempt_at':'started_at','last_sync_outcome':'outcome'}


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sync_attempts'").fetchone() is not None


def stamp(conn,key,default=None):
    row = conn.execute(f'SELECT {STAMPS[key]} FROM sync_attempts WHERE id=1').fetchone()
    if row is None or row[0] is None:
        return default
    return str(row[0])


def set_stamp(conn,key,value):
    column = STAMPS[key]
    value = value if column=='outcome' or value is None else float(value)
    conn.execute("INSERT INTO sync_attempts(id,revision,phase) VALUES (1,1,'running') ON CONFLICT(id) DO NOTHING")
    conn.execute(f'UPDATE sync_attempts SET {column}=?,revision=revision+1 WHERE id=1',(value,))


@collection_recovery.pinned_read
def read(conn):
    if not available(conn):
        from .preparation.library_sync_publication import RESULT
        revision = RESULT.selected(conn)
        if revision is not None:
            return json.loads(RESULT.value(conn,revision) or '{}')
        row = conn.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    attempt = conn.execute('SELECT * FROM sync_attempts WHERE id=1').fetchone()
    if attempt is None or attempt['recovery_filename'] is None:
        return {}
    result = collection_recovery.read(attempt['recovery_filename'],attempt['recovery_digest'],attempt['recovery_bytes'],
                                      kind='library-sync',owner=1)
    scopes = entity_names.owner(conn, 'playlist_scopes')
    for row in conn.execute(f'SELECT c.*,s.{scopes.column("provider_playlist_id")} AS provider_playlist_id FROM sync_cursors c '
                            f'LEFT JOIN {scopes.table} s ON s.{scopes.column("id")}=c.playlist_scope_id ORDER BY c.id'):
        account = (result.get('accounts',{}).get(str(row['account_id'])) if row['account_id'] is not None else result)
        if account is None:
            # Retired/omitted accounts retain their typed cursors, but do not
            # become configured accounts in a later attempt's report.
            continue
        if row['collection']=='account':
            item = account
        elif row['collection']=='playlist':
            item = account.setdefault('resources',{}).setdefault('playlists',{}).setdefault('items',{}).setdefault(row['provider_playlist_id'],{})
        else:
            item = account.setdefault('resources',{}).setdefault(row['collection'],{})
        for column,target in (('phase','status'),('updated_at','last_attempt_at'),('last_success_at','last_success_at'),
                              ('failures','failures'),('retry_at','retry_at')):
            if row[column] is None:
                item.pop(target,None)
            else:
                item[target] = row[column]
        if row['cursor'] is not None:
            item['cursor'] = row['cursor']
    result['last_attempt_at'] = attempt['finished_at']
    # A running attempt keeps the last published result, including freshness.
    if attempt['phase']!='running':
        result['status'] = attempt['phase']
    return result


def state(store):
    with store.read_view() as reader:
        return read(reader.conn)


def begin(conn,now):
    conn.execute("INSERT INTO sync_attempts(id,revision,phase,started_at) VALUES (1,1,'running',?) "
        "ON CONFLICT(id) DO UPDATE SET revision=revision+1,phase='running',started_at=excluded.started_at",(now,))
    return conn.execute('SELECT revision FROM sync_attempts WHERE id=1').fetchone()[0]


def _resources(result):
    for account_id,account in result.get('accounts',{}).items():
        if not isinstance(account_id,str) or not account_id.isdecimal() or not isinstance(account,dict):
            raise ValueError('Sync progress requires recorded account IDs')
        yield int(account_id),'account',None,account
        for name,item in account.get('resources',{}).items():
            if name not in RESOURCES:
                raise ValueError('Unsupported sync resource; original retained')
            yield int(account_id),name,None,item
            for provider_id,child in item.get('items',{}).items():
                if name!='playlists':
                    raise ValueError('Unsupported sync child progress; original retained')
                yield int(account_id),'playlist',provider_id,child
    for name,item in result.get('resources',{}).items():
        if name not in {'saved_album_mirror','album_tracks'}:
            raise ValueError('Unsupported global sync progress; original retained')
        yield None,name,None,item


def write(conn,result,now,*,last_success=None):
    if not isinstance(result,dict) or result.get('status') not in STATUSES:
        raise ValueError('Unsupported sync result; original retained')
    conn.execute('UPDATE sync_attempts SET revision=revision WHERE 0')
    rows = list(_resources(result))
    for account_id,collection,provider_id,item in rows:
        if not isinstance(item,dict) or item.get('status') not in STATUSES or (item.get('cursor') is not None and not isinstance(item['cursor'],str)):
            raise ValueError('Unsupported sync cursor; original retained')
        scope = playlist_scopes.ensure(conn,provider_id,account_id) if provider_id is not None else None
        old = conn.execute('SELECT id,revision FROM sync_cursors WHERE account_id IS ? AND provider=\'youtube\' '
                           'AND collection=? AND playlist_scope_id IS ?',(account_id,collection,scope)).fetchone()
        values = (item.get('cursor'),item['status'],item.get('last_attempt_at'),item.get('last_success_at'),
                  item.get('failures'),item.get('retry_at'))
        if old:
            conn.execute('UPDATE sync_cursors SET cursor=?,phase=?,updated_at=?,last_success_at=?,failures=?,retry_at=?,'
                         'revision=revision+1 WHERE id=?',(*values,old[0]))
        else:
            conn.execute('INSERT INTO sync_cursors(account_id,provider,collection,playlist_scope_id,revision,'
                'cursor,phase,updated_at,last_success_at,failures,retry_at) VALUES (?,\'youtube\',?,?,1,?,?,?,?,?,?)',
                (account_id,collection,scope,*values))
    filename,digest,size = collection_recovery.publish(conn,'library-sync',1,result)
    conn.execute('INSERT INTO sync_attempts(id,revision,phase,outcome,started_at,finished_at,last_success_at,'
        'recovery_filename,recovery_digest,recovery_bytes) VALUES (1,1,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET revision=revision+1,phase=excluded.phase,outcome=excluded.outcome,finished_at=excluded.finished_at,'
        'last_success_at=COALESCE(excluded.last_success_at,sync_attempts.last_success_at),'
        'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
        (result['status'],result['status'],now,result.get('last_attempt_at',now),last_success,filename,digest,size))
