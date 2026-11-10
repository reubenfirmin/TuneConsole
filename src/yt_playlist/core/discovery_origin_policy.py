"""Interpret captured discovery inputs without admitting missing subjects."""


def fields(token, facts, *, genre=None, account_id=None, import_key=None):
    if not isinstance(token, str) or not token:
        raise ValueError('Discovery needs its source input')
    result = dict(kind='legacy_unknown', account_id=account_id, album_id=None, seed_video_id=None,
                  genre_key=None, artist_id=None, import_key=None, legacy_source_key=token,
                  resolution_status='unresolved', resolution_reason='unrecognized source input')
    kind, separator, value = token.partition(':')
    if token in ('discovery', 'spotify_model'):
        result.update(kind=token, legacy_source_key=None, resolution_status='resolved', resolution_reason=None)
    elif kind == 'spotify_export' and separator:
        try:
            captured = int(value)
        except ValueError:
            captured = None
        if account_id is not None and captured != account_id:
            raise ValueError('Discovery import account disagrees with its input')
        if captured is not None and facts['account']:
            result.update(kind=kind, account_id=captured, import_key=import_key, legacy_source_key=None,
                          resolution_status='resolved' if import_key is not None else 'legacy_account', resolution_reason=None)
    elif separator and kind in ('radio', 'genre', 'artist', 'artist_start'):
        if kind == 'radio':
            target, column = facts['video'], 'seed_video_id'
        elif kind == 'genre':
            target, column = genre, 'genre_key'
        else:
            target = next(iter(facts['artists'])) if len(facts['artists']) == 1 else None
            column = 'artist_id'
        if target is not None:
            result.update(kind=kind, legacy_source_key=None, resolution_status='resolved', resolution_reason=None)
            result[column] = target
        else:
            result['resolution_reason'] = 'missing or ambiguous ' + kind + ' owner'
    elif not separator:
        if facts['album'] is not None:
            result.update(kind='album', album_id=facts['album'], legacy_source_key=None,
                          resolution_status='resolved', resolution_reason=None)
        else:
            result['resolution_reason'] = 'missing album owner'
    if result['kind'] not in ('spotify_export', 'spotify_model', 'legacy_unknown'):
        result['account_id'] = None
    return result
