"""Radio checkpoint ownership and revision fencing; no provider operations."""
import json
import time
from uuid import uuid4

from . import catalog, collection_recovery, playlist_scopes, radio_checkpoint_policy

SESSION_KEY = 'radio_current_session'
SELECTED_REVISION = 'SELECT id,revision FROM radio_sessions WHERE selected=1'


def write_checkpoint(conn, owner, account, scope, phase, revision, reference, now, operation, *, observed):
    changed = conn.execute('''
        INSERT INTO radio_sessions(id,account_id,playlist_scope_id,status,revision,recovery_filename,
            recovery_digest,recovery_bytes,updated_at,operation_json) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,playlist_scope_id=excluded.playlist_scope_id,
            status=excluded.status,revision=excluded.revision,recovery_filename=excluded.recovery_filename,
            recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes,
            updated_at=excluded.updated_at,operation_json=excluded.operation_json
        WHERE (radio_sessions.revision IS NULL AND excluded.revision=1)
            OR (radio_sessions.id=? AND radio_sessions.account_id=excluded.account_id
                AND radio_sessions.revision=excluded.revision-1)
    ''', (owner, account, scope, phase, revision, *reference, now, operation, observed)).rowcount
    if changed != 1:
        raise ValueError('Radio episode identity cannot be reassigned or resurrected')


def select_session(conn, owner):
    conn.execute('UPDATE radio_sessions SET selected=0 WHERE selected=1 AND id<>?', (owner,))
    conn.execute('UPDATE radio_sessions SET selected=1 WHERE id=? AND selected=0', (owner,))


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
        from . import radio_reads
        revision = radio_reads.session_selected(conn)
        if revision is not None:
            raw = radio_reads.session_value(conn,revision)
        else:
            row = conn.execute('SELECT value FROM settings WHERE key=?',(SESSION_KEY,)).fetchone()
            raw = row[0] if row else None
        value = json.loads(raw) if raw else None
        if project:
            from .append_reads import apply_radio
            apply_radio(conn,SESSION_KEY,value)
        return value,None
    row = conn.execute('SELECT * FROM radio_sessions WHERE selected=1').fetchone()
    if row is None:
        return None,None
    state = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                     kind='radio-session',owner=row['id'])
    if not project:
        return state,(row['id'],row['revision'])
    radio_checkpoint_policy.project_queue(state,row,list(conn.execute('SELECT q.*,r.identity_key FROM radio_queue_items q '
        'JOIN recordings r ON r.id=q.recording_id WHERE q.session_id=? ORDER BY q.position',(row['id'],))))
    radio_checkpoint_policy.restore_operation(state, row['operation_json'])
    return state,(row['id'],row['revision'])


def save(conn,state,*,expected,legacy=False,now=None):
    """Publish only against the caller's observed head, including a new episode."""
    conn.execute('UPDATE radio_sessions SET selected=selected WHERE 0')
    head = conn.execute(SELECTED_REVISION).fetchone()
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
            'SELECT 1 FROM accounts WHERE id=?',(account_id,)).fetchone():
        raise ValueError('Radio checkpoint requires its recorded account')
    existing = conn.execute('SELECT account_id,revision FROM radio_sessions WHERE id=? AND revision IS NOT NULL',
                            (owner,)).fetchone()
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
    operation = radio_checkpoint_policy.encode_operation(radio_checkpoint_policy.operation(state), revision)
    write_checkpoint(conn, owner, account_id, scope, phase, revision, (filename, digest, size),
                     time.time() if now is None else now, operation, observed=head[0] if head else None)
    conn.execute('DELETE FROM radio_queue_items WHERE session_id=?',(owner,))
    queue = [(track,'played' if position<state.get('position',-1) else
              'current' if position==state.get('position',-1) else 'queued')
             for position,track in enumerate(state.get('tracks',[]))]
    queue += [(track,'initial') for track in state.get('initial_tracks',[])]
    if state.get('pending'):
        queue.append((state['pending'],'pending'))
    for position,(track,status) in enumerate(queue):
        video = track['video_id']
        entry = conn.execute('SELECT id,recording_id FROM track_sources WHERE video_id=? ORDER BY id LIMIT 1',
                             (video,)).fetchone()
        if entry is None:
            key = '@unresolved-video:'+video
            entry_id = (catalog.admit_captured_video(conn,video,title=track.get('title',''),
                artist=track.get('artist',''),captured_at=now if now is not None else 0.) if legacy else
                catalog.upsert(conn,video,track.get('title',''),track.get('artist',''),preferred_key=key))
            entry = conn.execute('SELECT id,recording_id FROM track_sources WHERE id=?',(entry_id,)).fetchone()
        conn.execute('INSERT INTO radio_queue_items VALUES (?,?,?,?,?,?)',(owner,position,entry[1],entry[0],video,status))
    if head is None or head[0] != owner:
        select_session(conn, owner)
    from . import radio_policy_roots
    radio_policy_roots.save_session(conn, owner, radio_policy_roots.queue_versions(state))
    return owner,revision
