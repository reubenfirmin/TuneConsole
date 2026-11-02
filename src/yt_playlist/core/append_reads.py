"""Project retained append custody without importing preparation workers."""
from . import append_work, collection_recovery, radio_checkpoint


def _has(conn, table):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table,)).fetchone() is not None


def selected_operation(conn, name, source):
    native = (any(row[1] == 'playlist_scope_id' for row in conn.execute('PRAGMA table_info(rec_recipes)'))
              if name == 'tv_legacy' else _has(conn, 'radio_sessions') if name == 'radio_legacy'
              else append_work.available(conn))
    prefix = 'prepared_append_' + name
    if native or not _has(conn, prefix+'_publication_head'):
        return None
    row = conn.execute('SELECT revision FROM '+prefix+'_publication_head WHERE name=? AND active=1', (name,)).fetchone()
    if row is None:
        return None
    header = conn.execute('SELECT g.* FROM '+prefix+'_publication_map m JOIN '+prefix+'_generations g '
        'ON g.source=m.source AND g.preparation_revision=m.generation WHERE m.stage=? AND m.source=?',
        (row[0], str(source))).fetchone()
    return header if header is not None and header['operation_present'] else None


def sequence(conn, name, header, role, index=0):
    return [row[0] for row in conn.execute('SELECT video_id_snapshot FROM prepared_append_'+name+'_items '
        'WHERE source=? AND preparation_revision=? AND role=? AND list_position=? ORDER BY position',
        (header['source'], header['preparation_revision'], role, index))]


def apply_tv(conn, source, value):
    if not isinstance(value, dict) or append_work.available(conn):
        return value
    native = any(row[1] == 'playlist_scope_id' for row in conn.execute('PRAGMA table_info(rec_recipes)'))
    name = 'tv_native' if native else 'tv_legacy'
    header = selected_operation(conn, name, source)
    if header is None:
        return value
    checkpoint = collection_recovery.read(*(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes')),
        kind='tv-append', owner=header['source'])
    checkpoint.update(request_id=header['request_id'], state=header['state'])
    if 'at' in checkpoint:
        checkpoint['at'] = header['started_at']
    for bit, role in enumerate(('before', 'additions', 'items')):
        if header['role_mask'] & (1 << bit):
            videos = sequence(conn, name, header, role)
            checkpoint[role] = videos if role == 'before' else [
                {**item, 'video_id':video} for item, video in zip(checkpoint[role], videos, strict=True)]
    value.update(tv_stream=header['stream_token'], tv_refill=checkpoint)
    return value


def apply_radio(conn, source, value):
    if not isinstance(value, dict) or append_work.available(conn):
        return value
    name = 'radio_native' if _has(conn, 'radio_sessions') else 'radio_legacy'
    header = selected_operation(conn, name, source)
    if header is None:
        return value
    captured = collection_recovery.read(*(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes')),
        kind='radio-session', owner=header['source'])
    append = header['kind'] == 'append'
    operation = captured if append else captured['replacement']
    fields = (('pending_sent', 'sent'), ('pending_started_at', 'started_at')) if append else (
        ('waiting_at', 'started_at'), ('slot', 'slot'))
    for field, column in fields:
        if field in operation:
            operation[field] = bool(header[column]) if field == 'pending_sent' else header[column]
    receipt = operation.get('pending_result' if append else 'receipt')
    if receipt is not None:
        for field, column in (('status', 'receipt_status'), ('videoId', 'receipt_video_id_snapshot'), ('setVideoId', 'receipt_handle_snapshot')):
            if field in receipt:
                receipt[field] = header[column]
    if append:
        if operation.get('pending') is not None:
            operation['pending']['video_id'] = header['adding_video_id_snapshot']
        for field in ('pending', 'pending_sent', 'pending_started_at', 'pending_result'):
            if field in operation:
                value[field] = operation[field]
        return value
    if 'choice_id' in operation:
        operation['choice_id'] = radio_checkpoint.project_choice(operation['choice_id'], header['choice_id'])
    if 'allowed' in operation:
        operation['allowed'] = [sequence(conn, name, header, 'allowed', index) for index in range(header['allowed_count'])]
    for role in ('confirmed', 'waiting', 'play_prefix', 'excluded'):
        if role in operation and operation[role] is not None:
            operation[role] = sequence(conn, name, header, role)
    if 'proposed' in operation:
        operation['proposed'] = [{**item, 'video_id':video} for item, video in
            zip(operation['proposed'], sequence(conn, name, header, 'proposed'), strict=True)]
    if operation.get('adding') is not None:
        operation['adding']['video_id'] = header['adding_video_id_snapshot']
    value['replacement'] = operation
    return value
