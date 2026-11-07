"""Saved recipe validation and current relational projections over captured values."""
FIELDS = ('media', 'model', 'journey', 'opportunity_id', 'option_id')


def facets(value):
    if not isinstance(value, dict):
        raise ValueError('A saved playlist recipe requires an object')
    selections = {} if value.get('facets') is None else value['facets']
    if not isinstance(selections, dict):
        raise ValueError('Playlist recipe facets require an object')
    result = []
    for kind, field in (('genre', 'genres'), ('era', 'eras')):
        if field not in selections or selections[field] is None:
            continue
        if not isinstance(selections[field], list):
            raise ValueError('Playlist recipe facets require ordered lists')
        for position, label in enumerate(selections[field]):
            if not isinstance(label, str) or not label.strip():
                raise ValueError('Playlist recipe facets require nonempty labels')
            result.append((kind, position, kind + ':' + label, label))
    return result


def append_plan(value, owner):
    checkpoint = value.get('tv_refill')
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, dict) or not isinstance(value.get('tv_stream'), str) or not isinstance(checkpoint.get('request_id'), str):
        raise ValueError('Unsupported TV append recovery checkpoint')
    prior = owner['prior']
    if prior and prior['state'] == 'complete' and checkpoint['state'] != 'complete':
        raise ValueError('A completed TV append cannot become pending again')
    return {'id': owner['id'], 'stream': value['tv_stream'], 'request': checkpoint['request_id'],
            'state': checkpoint['state'], 'at': checkpoint.get('at', prior['started_at'] if prior else None),
            'checkpoint': checkpoint,
            'roles': {role: [item if role == 'before' else item['video_id'] for item in checkpoint[role]]
                      for role in ('before', 'additions', 'items') if role in checkpoint}}


def project(value, fields, facets):
    value.update({field: fields[field] for field in FIELDS if field in value})
    selections = {} if value.get('facets') is None else value['facets']
    for kind, field in (('genre', 'genres'), ('era', 'eras')):
        if field in selections and selections[field] is not None:
            selections[field] = [label for row_kind, label in facets if row_kind == kind]
    return value


def project_append(value, checkpoint, facts):
    header = facts['header']
    checkpoint.update(request_id=header['request_id'], state=header['state'])
    if 'at' in checkpoint:
        checkpoint['at'] = header['started_at']
    for bit, role in enumerate(('before', 'additions', 'items')):
        present = role in checkpoint if facts['native'] else header['role_mask'] & (1 << bit)
        if not present:
            continue
        videos = facts['roles'].get(role, [])
        if role == 'before':
            checkpoint[role] = videos
        elif facts['native']:
            metadata = {item['video_id']: item for item in checkpoint[role]}
            checkpoint[role] = [metadata.get(video, {'video_id': video}) for video in videos]
        else:
            checkpoint[role] = [{**item, 'video_id': video} for item, video in zip(checkpoint[role], videos, strict=True)]
    value.update(tv_stream=header['stream_token'], tv_refill=checkpoint)
    return value
