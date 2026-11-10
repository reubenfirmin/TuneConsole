"""Project retained append custody without importing preparation workers."""
from . import append_work, collection_recovery


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
    roles = {('allowed', index):sequence(conn,name,header,'allowed',index) for index in range(header['allowed_count'])}
    roles.update(((role,0),sequence(conn,name,header,role)) for role in ('confirmed','waiting','play_prefix','excluded','proposed'))
    from .radio_checkpoint_policy import project_prepared_operation
    project_prepared_operation(value,captured,header,roles)
    return value
