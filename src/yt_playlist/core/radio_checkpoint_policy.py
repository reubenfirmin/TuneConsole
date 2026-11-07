"""Radio checkpoint planning and projection without storage access."""
from . import radio_checkpoint


def queue(state):
    rows = [(track, 'played' if position < state.get('position', -1) else
             'current' if position == state.get('position', -1) else 'queued')
            for position, track in enumerate(state.get('tracks', []))]
    rows.extend((track, 'initial') for track in state.get('initial_tracks', []))
    if state.get('pending'):
        rows.append((state['pending'], 'pending'))
    return rows


def operation(state):
    pending, replacement = state.get('pending'), state.get('replacement')
    if pending and replacement:
        raise ValueError('A radio session can own only one active playlist operation')
    if not pending and not replacement:
        return None
    append = bool(pending)
    value = state if append else replacement
    receipt = (state.get('pending_result') if append else value.get('receipt')) or {}
    if not isinstance(receipt, dict):
        raise ValueError('Unsupported radio write receipt')
    allowed = [] if append else value.get('allowed', [])
    fields = ('append' if append else 'replacement',
        int(bool(state.get('pending_sent')) if append else value.get('waiting') is not None),
        state.get('pending_started_at') if append else value.get('waiting_at'),
        receipt.get('status'), receipt.get('videoId'), receipt.get('setVideoId'), len(allowed),
        int(not append and value.get('waiting') is not None),
        int(not append and value.get('play_prefix') is not None),
        pending['video_id'] if append else (value.get('adding') or {}).get('video_id'),
        None if append else value.get('slot'),
        None if append else radio_checkpoint.choice_key(value.get('choice_id')))
    lists = []
    if not append:
        lists = [('allowed', index, items) for index, items in enumerate(allowed)]
        lists.extend((role, 0, value.get(role) or []) for role in ('confirmed', 'waiting', 'play_prefix', 'excluded'))
        lists.append(('proposed', 0, [item['video_id'] for item in value.get('proposed', [])]))
    return {'fields': fields, 'items': [(role, index, position, video)
        for role, index, videos in lists for position, video in enumerate(videos)]}


def project_queue(state, header, rows):
    state.update(identity=header['account_id'], episode_id=header['id'], phase=header['status'])
    original = [*state.get('tracks', []), *state.get('initial_tracks', [])]
    if state.get('pending'):
        original.append(state['pending'])
    current, initial, pending = [], [], None
    for row in rows:
        track = dict(original[row['position']])
        track['video_id'] = row['video_id']
        for field in ('key', 'identity_key'):
            if field in track:
                track[field] = row['identity_key']
        if row['state'] == 'initial':
            initial.append(track)
        elif row['state'] == 'pending':
            pending = track
        else:
            current.append(track)
    state['tracks'] = current
    if 'initial_tracks' in state:
        state['initial_tracks'] = initial
    if 'pending' in state:
        state['pending'] = pending


def project_operation(state, header, roles):
    receipt = {field: header[column] for column, field in (
        ('receipt_status', 'status'), ('receipt_video_id_snapshot', 'videoId'),
        ('receipt_handle_snapshot', 'setVideoId')) if header[column] is not None}
    if header['kind'] == 'append':
        state.update(pending_sent=bool(header['sent']), pending_started_at=header['started_at'])
        if state.get('pending_result') is not None:
            state['pending_result'].update(receipt)
        return
    value = state['replacement']
    value.update(waiting_at=header['started_at'], slot=header['slot'],
        choice_id=radio_checkpoint.project_choice(value.get('choice_id'), header['choice_id']))
    if value.get('receipt') is not None:
        value['receipt'].update(receipt)
    value['allowed'] = [roles.get(('allowed', index), []) for index in range(header['allowed_count'])]
    for role, present in (('confirmed', True), ('waiting', header['waiting_present']),
                          ('play_prefix', header['play_prefix_present']), ('excluded', True)):
        if role in value:
            value[role] = roles.get((role, 0), []) if present else None
    metadata = {item['video_id']: item for item in value.get('proposed', [])}
    value['proposed'] = [metadata.get(video, {'video_id': video}) for video in roles.get(('proposed', 0), [])]
    if value.get('adding') is not None and header['adding_video_id_snapshot'] is not None:
        value['adding']['video_id'] = header['adding_video_id_snapshot']


def project_prepared_operation(state, captured, header, roles):
    append = header['kind'] == 'append'
    value = captured if append else captured['replacement']
    fields = (('pending_sent', 'sent'), ('pending_started_at', 'started_at')) if append else (
        ('waiting_at', 'started_at'), ('slot', 'slot'))
    for field, column in fields:
        if field in value:
            value[field] = bool(header[column]) if field == 'pending_sent' else header[column]
    receipt = value.get('pending_result' if append else 'receipt')
    if receipt is not None:
        for field, column in (('status', 'receipt_status'), ('videoId', 'receipt_video_id_snapshot'),
                              ('setVideoId', 'receipt_handle_snapshot')):
            if field in receipt:
                receipt[field] = header[column]
    if append:
        if value.get('pending') is not None:
            value['pending']['video_id'] = header['adding_video_id_snapshot']
        for field in ('pending', 'pending_sent', 'pending_started_at', 'pending_result'):
            if field in value:
                state[field] = value[field]
        return
    if 'choice_id' in value:
        value['choice_id'] = radio_checkpoint.project_choice(value['choice_id'], header['choice_id'])
    if 'allowed' in value:
        value['allowed'] = [roles.get(('allowed', index), []) for index in range(header['allowed_count'])]
    for role in ('confirmed', 'waiting', 'play_prefix', 'excluded'):
        if role in value and value[role] is not None:
            value[role] = roles.get((role, 0), [])
    if 'proposed' in value:
        value['proposed'] = [{**item, 'video_id': video} for item, video in
            zip(value['proposed'], roles.get(('proposed', 0), []), strict=True)]
    if value.get('adding') is not None:
        value['adding']['video_id'] = header['adding_video_id_snapshot']
    state['replacement'] = value
