"""Radio checkpoint ownership and revision fencing; no provider operations."""
import json
import time
from uuid import uuid4

from . import catalog, collection_recovery, entity_names, playlist_scopes

SESSION_KEY = 'radio_current_session'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_sessions'").fetchone() is not None


@collection_recovery.pinned_read
def load(conn):
    return _load(conn, project=True)


@collection_recovery.pinned_read
def load_published(conn):
    """Read the immutable checkpoint before current catalog/operation projections."""
    return _load(conn, project=False)


def _load(conn, *, project):
    if not available(conn):
        from .preparation import radio_publication
        revision = radio_publication.selected(conn)
        if revision is not None:
            raw = radio_publication.value(conn,revision)
        else:
            row = conn.execute('SELECT value FROM settings WHERE key=?',(SESSION_KEY,)).fetchone()
            raw = row[0] if row else None
        value = json.loads(raw) if raw else None
        if project:
            from .preparation.append_publication import apply_radio
            apply_radio(conn,SESSION_KEY,value)
        return value,None
    row = conn.execute('SELECT s.* FROM radio_session_head h JOIN radio_sessions s ON s.id=h.session_id').fetchone()
    if row is None:
        return None,None
    state = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='radio-session',owner=row['id'])
    if not project:
        return state,(row['id'],row['revision'])
    state.update(identity=row['account_id'],episode_id=row['id'],phase=row['status'])
    original = [*state.get('tracks',[]),*state.get('initial_tracks',[])]
    if state.get('pending'):
        original.append(state['pending'])
    current,initial,pending = [],[],None
    for item in conn.execute('SELECT q.*,r.identity_key FROM radio_queue_items q '
            'JOIN recordings r ON r.id=q.recording_id WHERE q.session_id=? ORDER BY q.position',(row['id'],)):
        track = dict(original[item['position']])
        track['video_id'] = item['video_id']
        for field in ('key','identity_key'):
            if field in track:
                track[field] = item['identity_key']
        if item['state']=='initial':
            initial.append(track)
        elif item['state']=='pending':
            pending = track
        else:
            current.append(track)
    state['tracks'] = current
    if 'initial_tracks' in state:
        state['initial_tracks'] = initial
    if 'pending' in state:
        state['pending'] = pending
    from . import append_work
    if append_work.available(conn):
        append_work.load_radio(conn,row['id'],state)
    else:
        from .preparation.append_publication import apply_radio
        apply_radio(conn,row['id'],state)
    return state,(row['id'],row['revision'])


def save(conn,state,*,expected,legacy=False,now=None):
    """Publish only against the caller's observed head, including a new episode."""
    conn.execute('UPDATE radio_session_head SET session_id=session_id WHERE 0')
    head = conn.execute('SELECT s.id,s.revision FROM radio_session_head h '
                        'JOIN radio_sessions s ON s.id=h.session_id').fetchone()
    observed = tuple(head) if head else None
    if observed!=expected:
        raise ValueError('Radio session changed in another worker; reload its checkpoint')
    owner = state.get('episode_id')
    if not isinstance(owner,str) or not owner:
        if not legacy:
            raise ValueError('Radio checkpoint requires an episode ID')
        owner = 'legacy:'+uuid4().hex
        state = {**state,'episode_id':owner}
    account_id = state.get('identity')
    if type(account_id) is not int or not conn.execute(
            'SELECT 1 FROM '+entity_names.accounts(conn)+' WHERE id=?',(account_id,)).fetchone():
        raise ValueError('Radio checkpoint requires its recorded account')
    existing = conn.execute('SELECT account_id,revision FROM radio_sessions WHERE id=?',(owner,)).fetchone()
    if existing and (existing[0]!=account_id or not head or head[0]!=owner):
        raise ValueError('Radio episode identity cannot be reassigned or resurrected')
    revision = existing[1]+1 if existing else 1
    scope = playlist_scopes.ensure(conn,state['playlist'],account_id) if state.get('playlist') else None
    phase = state.get('phase')
    if state.get('version')==1:
        # Use the versioned wire decoder; it performs no I/O.
        from .radio_checkpoint import normalize
        phase = normalize(state)['phase']
    filename,digest,size = collection_recovery.publish(conn,'radio-session',owner,state)
    conn.execute('INSERT INTO radio_sessions(id,account_id,playlist_scope_id,status,revision,recovery_filename,'
        'recovery_digest,recovery_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET playlist_scope_id=excluded.playlist_scope_id,status=excluded.status,'
        'revision=excluded.revision,recovery_filename=excluded.recovery_filename,'
        'recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes,updated_at=excluded.updated_at',
        (owner,account_id,scope,phase,revision,filename,digest,size,time.time() if now is None else now))
    conn.execute('DELETE FROM radio_queue_items WHERE session_id=?',(owner,))
    queue = [(track,'played' if position<state.get('position',-1) else
              'current' if position==state.get('position',-1) else 'queued')
             for position,track in enumerate(state.get('tracks',[]))]
    queue += [(track,'initial') for track in state.get('initial_tracks',[])]
    if state.get('pending'):
        queue.append((state['pending'],'pending'))
    for position,(track,status) in enumerate(queue):
        video = track['video_id']
        entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1',
                             (video,)).fetchone()
        if entry is None:
            key = '@unresolved-video:'+video
            entry_id = catalog.upsert(conn,video,track.get('title',''),track.get('artist',''),preferred_key=key)
            entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE id=?',(entry_id,)).fetchone()
        conn.execute('INSERT INTO radio_queue_items VALUES (?,?,?,?,?,?)',(owner,position,entry[1],entry[0],video,status))
    conn.execute('INSERT INTO radio_session_head VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET session_id=excluded.session_id',
                 (owner,))
    from . import radio_policy_roots
    radio_policy_roots.save_session(conn, owner, state)
    from . import append_work
    if append_work.available(conn):
        append_work.save_radio(conn,owner,revision,state)
    return owner,revision
