"""Grouping progress and the exact generation checked by its worker."""
import json

KEYS = frozenset({'rec_group_shadow_progress','rec_group_shadow_checkpoint','rec_mode_fit_inputs'})


def available(conn):
    from . import checkpoint_reads as checkpoint_publication
    return (conn.execute("SELECT 1 FROM sqlite_schema WHERE name='group_preparation_state'").fetchone() is not None
            or checkpoint_publication.installed(conn, 'group-checkpoints'))


def get(conn,key,default=None):
    from . import group_progress
    from . import checkpoint_reads as checkpoint_publication
    revision = checkpoint_publication.selected(conn, 'group-checkpoints')
    if revision is not None:
        return checkpoint_publication.read(conn, 'group-checkpoints', revision, key, default)
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone():
        return checkpoint_publication.fallback(conn, key, default)
    if key == 'rec_group_shadow_progress' and group_progress.available(conn):
        return group_progress.read(conn, default)
    row = conn.execute('SELECT s.*,g.external_key FROM group_preparation_state s '
                       'LEFT JOIN group_generations g ON g.id=s.generation_id WHERE singleton=1').fetchone()
    if row is None:
        return default
    if key=='rec_mode_fit_inputs':
        return row['fit_fingerprint'] if row['fit_fingerprint'] is not None else default
    if key=='rec_group_shadow_progress':
        return json.dumps({'phase':row['phase'],'at':row['reported_at'],'reason':row['reason']}) if row['phase'] else default
    return json.dumps({'generation':row['external_key'] or row['unresolved_generation'],'serial':row['input_serial'],
                       'at':row['checkpoint_at']}) if row['checkpoint_at'] is not None else default


def put(conn,values,*,legacy=False):
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone():
        from .control_reads import retained_write
        return retained_write(conn, values)
    if legacy:
        # Explicit frozen conversion may run without prior regular admission.
        # Adopt the bounded replacement without changing the frozen v94 DDL.
        from . import group_progress
        group_progress.install(conn)
    if not values:
        return
    conn.execute('INSERT INTO group_preparation_state(singleton) VALUES (1) ON CONFLICT(singleton) DO NOTHING')
    for key,raw in values.items():
        if key=='rec_mode_fit_inputs':
            conn.execute('UPDATE group_preparation_state SET fit_fingerprint=? WHERE singleton=1',(raw,))
            continue
        value = json.loads(raw) if raw else {}
        if not isinstance(value,dict):
            raise ValueError('Grouping progress must be a supported object')
        if key=='rec_group_shadow_progress':
            if set(value)-{'phase','at','reason'}:
                raise ValueError('Unsupported grouping progress; original retained')
            from . import group_progress
            # Deferred admission keeps continuing writers usable before the
            # regular runner has adopted this bounded singleton replacement.
            group_progress.install(conn)
            group_progress.write(conn, value)
        else:
            if set(value)-{'generation','serial','at'}:
                raise ValueError('Unsupported grouping checkpoint; original retained')
            checkpoint(conn,value.get('generation'),value.get('serial'),value.get('at'),legacy=legacy)


def checkpoint(conn,generation,serial,now,*,legacy=False):
    row = conn.execute('SELECT id FROM group_generations WHERE external_key=?',(generation,)).fetchone()
    if generation is not None and row is None and not legacy:
        raise ValueError('Grouping checkpoint requires its published generation')
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='group_preparation_state'").fetchone():
        from .control_reads import retained_write
        return retained_write(conn, {'rec_group_shadow_checkpoint': json.dumps(
            {'generation': generation, 'serial': serial, 'at': now})})
    conn.execute('INSERT INTO group_preparation_state(singleton) VALUES (1) ON CONFLICT(singleton) DO NOTHING')
    conn.execute('UPDATE group_preparation_state SET generation_id=?,unresolved_generation=?,input_serial=?,checkpoint_at=? '
                 'WHERE singleton=1',(row[0] if row else None,generation if row is None else None,serial,now))
