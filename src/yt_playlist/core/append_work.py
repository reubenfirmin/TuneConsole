"""Typed TV append and radio edit admission; exact metadata stays owner-file backed."""
from . import collection_recovery


def available(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tv_append_intents'").fetchone())


def save_tv(conn,recipe_id,scope,value):
    checkpoint = value.get('tv_refill')
    if checkpoint is None:
        return
    if not isinstance(checkpoint,dict) or not isinstance(value.get('tv_stream'),str) or not isinstance(checkpoint.get('request_id'),str):
        raise ValueError('Unsupported TV append recovery checkpoint')
    prior = conn.execute('SELECT * FROM tv_append_intents WHERE playlist_scope_id=? AND stream_token=? AND request_id=?',
                         (scope,value['tv_stream'],checkpoint['request_id'])).fetchone()
    identity = prior['id'] if prior else conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM tv_append_intents').fetchone()[0]
    if prior and prior['state']=='complete' and checkpoint['state']!='complete':
        raise ValueError('A completed TV append cannot become pending again')
    reference = collection_recovery.publish(conn,'tv-append',identity,checkpoint)
    plan = {'id':identity, 'stream':value['tv_stream'], 'request':checkpoint['request_id'],
            'state':checkpoint['state'], 'at':checkpoint.get('at',prior['started_at'] if prior else None),
            'reference':reference, 'roles':{role:[item if role=='before' else item['video_id'] for item in checkpoint[role]]
                for role in ('before','additions','items') if role in checkpoint}}
    install_tv(conn,recipe_id,scope,plan)


def install_tv(conn,recipe_id,scope,plan):
    """Install prepared append references and preserve omitted original role rows."""
    identity = plan['id']
    conn.execute('INSERT INTO tv_append_intents VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
        'state=excluded.state,recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
        'recovery_bytes=excluded.recovery_bytes',
        (identity,recipe_id,scope,plan['stream'],plan['request'],plan['state'],plan['at'],*plan['reference']))
    # Retain the original before/intent after completion even though the compact
    # compatibility response no longer includes those fields.
    for role,videos in plan['roles'].items():
        conn.execute('DELETE FROM tv_append_items WHERE intent_id=? AND role=?',(identity,role))
        conn.executemany('INSERT INTO tv_append_items VALUES (?,?,?,?)',
            [(identity,role,index,video) for index,video in enumerate(videos)])


def load_tv(conn,recipe_id,value):
    checkpoint = value.get('tv_refill')
    if not isinstance(checkpoint,dict):
        return
    row = conn.execute('SELECT * FROM tv_append_intents WHERE recipe_id=? AND stream_token=? AND request_id=?',
                       (recipe_id,value.get('tv_stream'),checkpoint.get('request_id'))).fetchone()
    if row is None:
        raise ValueError('Saved TV append has no retained intent')
    captured = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='tv-append',owner=row['id'])
    captured.update(request_id=row['request_id'],state=row['state'])
    if 'at' in captured:
        captured['at'] = row['started_at']
    for role in ('before','additions','items'):
        if role not in captured:
            continue
        videos = [item[0] for item in conn.execute('SELECT video_id_snapshot FROM tv_append_items WHERE intent_id=? AND role=? ORDER BY position',(row['id'],role))]
        metadata = {item['video_id']:item for item in captured[role]} if role!='before' else {}
        captured[role] = videos if role=='before' else [metadata.get(video,{'video_id':video}) for video in videos]
    value['tv_refill'] = captured


def save_radio(conn,owner,revision,state):
    from .radio_checkpoint_policy import operation
    install_radio(conn,owner,revision,operation(state))


def install_radio(conn,owner,revision,plan):
    """Install prepared operation facts in the checkpoint owner's SQL write."""
    if plan is None:
        conn.execute('DELETE FROM radio_operations WHERE session_id=?',(owner,))
        return
    conn.execute('INSERT INTO radio_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(session_id) DO UPDATE SET session_revision=excluded.session_revision,kind=excluded.kind,sent=excluded.sent,'
        'started_at=excluded.started_at,receipt_status=excluded.receipt_status,receipt_video_id_snapshot=excluded.receipt_video_id_snapshot,'
        'receipt_handle_snapshot=excluded.receipt_handle_snapshot,allowed_count=excluded.allowed_count,waiting_present=excluded.waiting_present,'
        'play_prefix_present=excluded.play_prefix_present,adding_video_id_snapshot=excluded.adding_video_id_snapshot,slot=excluded.slot,choice_id=excluded.choice_id',
        (owner,revision,*plan['fields']))
    conn.execute('DELETE FROM radio_operation_items WHERE session_id=?',(owner,))
    conn.executemany('INSERT INTO radio_operation_items VALUES (?,?,?,?,?)',[(owner,*row) for row in plan['items']])


def load_radio(conn,owner,state):
    row = conn.execute('SELECT * FROM radio_operations WHERE session_id=?',(owner,)).fetchone()
    if row is None:
        return
    roles = {}
    for item in conn.execute('SELECT * FROM radio_operation_items WHERE session_id=? ORDER BY role,list_position,position',(owner,)):
        roles.setdefault((item['role'],item['list_position']),[]).append(item['video_id_snapshot'])
    from .radio_checkpoint_policy import project_operation
    project_operation(state,row,roles)
