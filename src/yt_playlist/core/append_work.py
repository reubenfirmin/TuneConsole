"""TV append checkpoints stored on their saved playlist scope."""
import json


COLUMN = 'tv_appends'
DECLARATION = f"{COLUMN} TEXT CHECK({COLUMN} IS NULL OR json_type({COLUMN}) IS 'array')"


def entries(conn, scope):
    row = conn.execute(f'SELECT {COLUMN} FROM playlist_scopes WHERE id=?', (scope,)).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else []


def save(conn, scope, checkpoints):
    conn.execute(f'UPDATE playlist_scopes SET {COLUMN}=? WHERE id=?', (json.dumps(checkpoints), scope))


def _matches(row, stream, request):
    return row['stream_token'] == stream and row['request_id'] == request


def prepare(recipe):
    checkpoint = recipe.get('tv_refill')
    if checkpoint is None:
        return recipe, None
    stream = recipe.get('tv_stream')
    if (not isinstance(checkpoint, dict) or not isinstance(stream, str)
            or not isinstance(checkpoint.get('request_id'), str) or checkpoint.get('state') not in ('pending', 'complete')):
        raise ValueError('Unsupported TV append recovery checkpoint')
    request = checkpoint['request_id']
    return {**recipe, 'tv_refill': {'request_id': request}}, {
        'stream_token': stream, 'request_id': request, 'state': checkpoint['state'], 'checkpoint': checkpoint}


def install(conn, scope, row):
    if row is None:
        return
    checkpoints = entries(conn, scope)
    for index, prior in enumerate(checkpoints):
        if not _matches(prior, row['stream_token'], row['request_id']):
            continue
        _require_forward(prior, row)
        checkpoints[index] = {**prior, **row}
        break
    else:
        checkpoints.append(row)
    save(conn, scope, checkpoints)


def _require_forward(prior, row):
    if prior['state'] == 'complete' and row['state'] != 'complete':
        raise ValueError('A completed TV append cannot become pending again')


def check(conn, scope, row):
    for prior in entries(conn, scope):
        if _matches(prior, row['stream_token'], row['request_id']):
            _require_forward(prior, row)
            break


def project(recipe, checkpoints):
    saved = recipe.get('tv_refill')
    if isinstance(saved, dict):
        row = next((row for row in checkpoints if _matches(row, recipe.get('tv_stream'), saved.get('request_id'))), None)
        if row is None:
            raise ValueError('Saved TV append has no retained intent')
        recipe['tv_refill'] = row['checkpoint']
    return recipe
