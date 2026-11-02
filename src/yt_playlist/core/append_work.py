"""Typed TV append and radio edit admission; exact metadata stays owner-file backed."""
from . import collection_recovery, radio_checkpoint


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
    conn.execute('INSERT INTO tv_append_intents VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
        'state=excluded.state,recovery_filename=excluded.recovery_filename,recovery_digest=excluded.recovery_digest,'
        'recovery_bytes=excluded.recovery_bytes',
        (identity,recipe_id,scope,value['tv_stream'],checkpoint['request_id'],checkpoint['state'],
         checkpoint.get('at',prior['started_at'] if prior else None),*reference))
    # Retain the original before/intent after completion even though the compact
    # compatibility response no longer includes those fields.
    for role in ('before','additions','items'):
        if role not in checkpoint:
            continue
        conn.execute('DELETE FROM tv_append_items WHERE intent_id=? AND role=?',(identity,role))
        conn.executemany('INSERT INTO tv_append_items VALUES (?,?,?,?)',
            [(identity,role,index,item if role=='before' else item['video_id']) for index,item in enumerate(checkpoint[role])])


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
    pending,replacement = state.get('pending'),state.get('replacement')
    if pending and replacement:
        raise ValueError('A radio session can own only one active playlist operation')
    if not pending and not replacement:
        conn.execute('DELETE FROM radio_operations WHERE session_id=?',(owner,))
        return
    append = bool(pending)
    operation = state if append else replacement
    receipt = (state.get('pending_result') if append else operation.get('receipt')) or {}
    if not isinstance(receipt,dict):
        raise ValueError('Unsupported radio write receipt')
    allowed = [] if append else operation.get('allowed',[])
    conn.execute('INSERT INTO radio_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(session_id) DO UPDATE SET session_revision=excluded.session_revision,kind=excluded.kind,sent=excluded.sent,'
        'started_at=excluded.started_at,receipt_status=excluded.receipt_status,receipt_video_id_snapshot=excluded.receipt_video_id_snapshot,'
        'receipt_handle_snapshot=excluded.receipt_handle_snapshot,allowed_count=excluded.allowed_count,waiting_present=excluded.waiting_present,'
        'play_prefix_present=excluded.play_prefix_present,adding_video_id_snapshot=excluded.adding_video_id_snapshot,slot=excluded.slot,choice_id=excluded.choice_id',
        (owner,revision,'append' if append else 'replacement',int(bool(state.get('pending_sent')) if append else operation.get('waiting') is not None),
         state.get('pending_started_at') if append else operation.get('waiting_at'),receipt.get('status'),receipt.get('videoId'),receipt.get('setVideoId'),
         len(allowed),int(not append and operation.get('waiting') is not None),int(not append and operation.get('play_prefix') is not None),
         (operation.get('adding') or {}).get('video_id') if not append else pending['video_id'],
         None if append else operation.get('slot'),None if append else radio_checkpoint.choice_key(operation.get('choice_id'))))
    conn.execute('DELETE FROM radio_operation_items WHERE session_id=?',(owner,))
    if not append:
        lists = [('allowed',index,items) for index,items in enumerate(allowed)]
        lists += [(role,0,operation.get(role) or []) for role in ('confirmed','waiting','play_prefix','excluded')]
        lists.append(('proposed',0,[item['video_id'] for item in operation.get('proposed',[])]))
        for role,index,items in lists:
            conn.executemany('INSERT INTO radio_operation_items VALUES (?,?,?,?,?)',[(owner,role,index,position,video) for position,video in enumerate(items)])


def load_radio(conn,owner,state):
    row = conn.execute('SELECT * FROM radio_operations WHERE session_id=?',(owner,)).fetchone()
    if row is None:
        return
    receipt = {field:row[column] for column,field in (('receipt_status','status'),('receipt_video_id_snapshot','videoId'),('receipt_handle_snapshot','setVideoId')) if row[column] is not None}
    if row['kind']=='append':
        state.update(pending_sent=bool(row['sent']),pending_started_at=row['started_at'])
        if state.get('pending_result') is not None:
            state['pending_result'].update(receipt)
        return
    operation = state['replacement']
    operation.update(waiting_at=row['started_at'],slot=row['slot'],
        choice_id=radio_checkpoint.project_choice(operation.get('choice_id'),row['choice_id']))
    if operation.get('receipt') is not None:
        operation['receipt'].update(receipt)
    items = list(conn.execute('SELECT * FROM radio_operation_items WHERE session_id=? ORDER BY role,list_position,position',(owner,)))
    def sequence(role,index=0):
        return [item['video_id_snapshot'] for item in items if item['role']==role and item['list_position']==index]
    operation['allowed'] = [sequence('allowed',index) for index in range(row['allowed_count'])]
    for role,flag in (('confirmed',True),('waiting',row['waiting_present']),('play_prefix',row['play_prefix_present']),('excluded',True)):
        if role in operation:
            operation[role] = sequence(role) if flag else None
    metadata = {item['video_id']:item for item in operation.get('proposed',[])}
    operation['proposed'] = [metadata.get(video,{'video_id':video}) for video in sequence('proposed')]
    if operation.get('adding') is not None and row['adding_video_id_snapshot'] is not None:
        operation['adding']['video_id'] = row['adding_video_id_snapshot']
