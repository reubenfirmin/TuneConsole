"""Finite compatibility parsers for retired GC and Spotify workflow settings."""
import json
from uuid import uuid4

from . import collection_recovery, playlist_scopes

GC = 'playlist_gc_retry:'
PLAYLIST = 'spotify_library_imported_playlists'
ALBUM = 'spotify_library_imported_albums'
TOKEN = '_receipt_revision'


def available(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spotify_transfer_receipts'").fetchone())


def owns(key):
    return key.startswith(GC) or any(key==prefix or key.startswith(prefix+':') for prefix in (PLAYLIST,ALBUM))


def scope(conn,account,remote,*,legacy=False):
    from . import entity_names
    known = conn.execute(f'SELECT id FROM {entity_names.accounts(conn)} WHERE id=?',(account,)).fetchone() if account is not None else None
    if not known and not legacy:
        raise ValueError('New workflow receipts require their retained account')
    return (known[0],playlist_scopes.ensure(conn,remote,known[0]) if remote else None) if known else (None,None)


def _gc_key(account,remote):
    return GC+json.dumps([account,remote],separators=(',',':'))


def _gc_owner(key):
    pair = json.loads(key[len(GC):])
    if not isinstance(pair,list) or len(pair)!=2 or type(pair[0]) is not int or not isinstance(pair[1],str) or not pair[1]:
        raise ValueError('Unsupported GC account/playlist receipt key')
    return pair


def gc_save(conn,account,remote,value,*,legacy=False):
    conn.execute('UPDATE playlist_gc_attempts SET revision=revision WHERE 0')
    prior = conn.execute('SELECT id FROM playlist_gc_attempts WHERE captured_account_id=? AND provider_playlist_id_snapshot=?',
                         (account,remote)).fetchone()
    if value is None:
        conn.execute('DELETE FROM playlist_gc_attempts WHERE captured_account_id=? AND provider_playlist_id_snapshot=?',(account,remote))
        return
    if not isinstance(value,dict) or value.get('identity')!=account or value.get('ytm')!=remote:
        raise ValueError('GC receipt disagrees with its captured target')
    known,target_scope = scope(conn,account,remote,legacy=legacy)
    identity = prior[0] if prior else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM playlist_gc_attempts').fetchone()[0]
    reference = collection_recovery.publish(conn,'playlist-gc',identity,value)
    conn.execute('INSERT INTO playlist_gc_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
        'revision=excluded.revision,attempts=excluded.attempts,last_attempt_at=excluded.last_attempt_at,next_retry_at=excluded.next_retry_at,'
        'phase=excluded.phase,failure_class=excluded.failure_class,error=excluded.error,recovery_filename=excluded.recovery_filename,'
        'recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
        (identity,account,known,target_scope,remote,uuid4().hex,value['attempts'],value['last_attempt_at'],value['next_retry_at'],
         value.get('phase'),value.get('failure_class'),value.get('error'),*reference))


@collection_recovery.pinned_read
def gc_values(conn):
    result = {}
    for row in conn.execute('SELECT * FROM playlist_gc_attempts ORDER BY id'):
        value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='playlist-gc',owner=row['id'])
        for field in ('attempts','last_attempt_at','next_retry_at','phase','failure_class','error'):
            if field in value:
                value[field] = row[field]
        result[_gc_key(row['captured_account_id'],row['provider_playlist_id_snapshot'])] = value
    return result


def receipt_save(conn,kind,account,fingerprint,value,*,legacy=False,expected_revision=None):
    conn.execute('UPDATE spotify_transfer_receipts SET revision=revision WHERE 0')
    prior = conn.execute('SELECT * FROM spotify_transfer_receipts WHERE source_kind=? AND captured_account_id IS ? AND fingerprint=?',
                         (kind,account,fingerprint)).fetchone()
    if (expected_revision=='' and prior is not None) or (expected_revision not in (None,'') and (prior is None or prior['revision']!=expected_revision)):
        raise ValueError('Spotify transfer completion belongs to a stale revision')
    known,target_scope = scope(conn,account,value.get('ytm_playlist_id') if isinstance(value,dict) else None,legacy=legacy)
    identity = prior['id'] if prior else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM spotify_transfer_receipts').fetchone()[0]
    if not isinstance(value,(dict,type(None))):
        raise ValueError('Unsupported Spotify receipt format')
    state = dict(value or {})
    state.pop(TOKEN,None)
    reference = collection_recovery.publish(conn,'spotify-transfer',identity,value if value is None else state)
    revision = uuid4().hex
    phase = state.get('creation_phase','legacy')
    if prior and ((prior['marker'] is not None and prior['marker']!=state.get('marker')) or
                  (prior['provider_playlist_id_snapshot'] is not None and prior['provider_playlist_id_snapshot']!=state.get('ytm_playlist_id'))):
        raise ValueError('A Spotify receipt cannot change its creation marker or destination')
    conn.execute('INSERT INTO spotify_transfer_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,creation_phase=excluded.creation_phase,marker=excluded.marker,'
        'playlist_scope_id=excluded.playlist_scope_id,provider_playlist_id_snapshot=excluded.provider_playlist_id_snapshot,'
        'complete=excluded.complete,order_verified=excluded.order_verified,updated_at=excluded.updated_at,last_run_added=excluded.last_run_added,'
        'recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes',
        (identity,kind,account,known,fingerprint,revision,phase,state.get('marker'),target_scope,state.get('ytm_playlist_id'),
         state.get('complete'),state.get('order_verified'),state.get('updated_at'),state.get('last_run_added'),*reference))
    positions = state.get('source_video_ids',[])
    if not isinstance(positions,list) or any(v is not None and (not isinstance(v,str) or not v) for v in positions):
        raise ValueError('Unsupported Spotify ordered source slots')
    conn.execute('DELETE FROM spotify_transfer_items WHERE receipt_id=?',(identity,))
    conn.executemany('INSERT INTO spotify_transfer_items VALUES (?,?,?)',[(identity,i,v) for i,v in enumerate(positions)])
    return revision


@collection_recovery.pinned_read
def receipts(conn,kind,account,*,token=False):
    result = {}
    for row in conn.execute('SELECT * FROM spotify_transfer_receipts WHERE source_kind=? AND captured_account_id IS ? ORDER BY id',(kind,account)):
        value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='spotify-transfer',owner=row['id'])
        if isinstance(value,dict):
            for column,field in (('creation_phase','creation_phase'),('marker','marker'),('provider_playlist_id_snapshot','provider_playlist_id'),
                                  ('complete','complete'),('order_verified','order_verified'),('updated_at','updated_at'),('last_run_added','last_run_added')):
                if field in value:
                    value[field] = bool(row[column]) if field in ('complete','order_verified') and row[column] is not None else row[column]
            if 'source_video_ids' in value:
                value['source_video_ids'] = [item[0] for item in conn.execute('SELECT video_id_snapshot FROM spotify_transfer_items WHERE receipt_id=? ORDER BY position',(row['id'],))]
            if token:
                value[TOKEN] = row['revision']
        result[row['fingerprint']] = value
    return result


def receipt_key(key):
    prefix = next(p for p in (PLAYLIST,ALBUM) if key==p or key.startswith(p+':'))
    account = None if key==prefix else int(key[len(prefix)+1:])
    return ('playlist' if prefix==PLAYLIST else 'album'),account


def put(conn,values,*,legacy=False):
    for key,raw in values.items():
        value = json.loads(raw) if raw is not None and raw!='' else None
        if key.startswith(GC):
            gc_save(conn,*_gc_owner(key),value,legacy=legacy)
        else:
            kind,account = receipt_key(key)
            if value is None:
                conn.execute('DELETE FROM spotify_transfer_receipts WHERE source_kind=? AND captured_account_id IS ?',(kind,account))
            elif isinstance(value,list) and all(isinstance(item,str) for item in value):
                for fingerprint in value:
                    receipt_save(conn,kind,account,fingerprint,None,legacy=legacy or account is None)
            elif isinstance(value,dict) and kind=='playlist':
                for fingerprint,item in value.items():
                    receipt_save(conn,kind,account,fingerprint,item,legacy=legacy)
            else:
                raise ValueError('Unsupported Spotify workflow receipt family')


def get(conn,key,default=None):
    if key.startswith(GC):
        value = gc_values(conn).get(key)
        return json.dumps(value) if value is not None else default
    kind,account = receipt_key(key)
    value = receipts(conn,kind,account)
    if not value:
        return default
    return json.dumps(sorted(value) if kind=='album' or account is None else value)


def settings_with_prefix(conn,prefix):
    values = {key:json.dumps(value) for key,value in gc_values(conn).items() if key.startswith(prefix)}
    for kind,account in conn.execute('SELECT DISTINCT source_kind,captured_account_id FROM spotify_transfer_receipts'):
        key = (PLAYLIST if kind=='playlist' else ALBUM)+('' if account is None else ':'+str(account))
        if key.startswith(prefix):
            values[key] = get(conn,key)
    return values
