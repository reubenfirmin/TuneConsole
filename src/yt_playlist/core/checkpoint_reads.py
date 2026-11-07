"""Selected finite model checkpoints, independent of conversion workers."""
import json

from . import content_checkpoint_contract as content

HEAD = 'published_checkpoint_heads'
NATIVE = {'content-checkpoints':'content_basis_checkpoint', 'group-checkpoints':'group_preparation_state',
          'candidate-checkpoints':'candidate_refresh_checkpoint'}
TABLES = {key:table.replace('prepared_', 'published_', 1) for key,table in content.PREPARED.items()}
LEGACY_PROGRESS = 'published_group_progress_checkpoint'
PROGRESS_V2 = TABLES[content.GROUP[0]]


def installed(conn, name):
    return (conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone() is not None
            and conn.execute(f'SELECT 1 FROM {HEAD} WHERE name=?', (name,)).fetchone() is not None)


def selected(conn, name):
    if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (NATIVE[name],)).fetchone():
        return None
    if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (HEAD,)).fetchone():
        return None
    row = conn.execute(f'SELECT revision FROM {HEAD} WHERE name=? AND active=1', (name,)).fetchone()
    return row[0] if row else None


def progress_row(conn, name, revision):
    for table in (PROGRESS_V2, LEGACY_PROGRESS):
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (table,)).fetchone():
            row = conn.execute(f'SELECT * FROM {table} WHERE name=? AND revision=? AND source=?',
                               (name, revision, content.GROUP[0])).fetchone()
            if row is not None:
                return row
    return None


def read(conn, name, revision, key, default):
    row = (progress_row(conn, name, revision) if key == content.GROUP[0] else conn.execute(
        f'SELECT * FROM {TABLES[key]} WHERE name=? AND revision=? AND source=?', (name, revision, key)).fetchone())
    if row is None:
        return default
    if key == 'rec_mode_fit_inputs':
        return row['fit_fingerprint'] if row['source_kind'] == 'recorded' else '' if row['source_kind'] == 'blank' else default
    if key in content.CANDIDATES:
        if key == 'rec_candidate_preferences' and row['source_kind'] == 'blank':
            return ''
        field = 'expires_at' if key == 'rec_candidates_expire_at' else 'preferences_fingerprint'
        return str(row[field]) if row[field] is not None else default
    if row['source_kind'] != 'recorded':
        return default
    if key in content.KEYS:
        value = {field:row[field] for field in content.FIELDS[key] if row[field] is not None}
        if key == 'rec_preparation_full':
            value['at'] = row['at']
            if row['has_config']:
                value['config'] = [row[field] for field in content.CONFIG]
        else:
            if row['has_phases']:
                value['phase_bytes'] = {phase:row['bytes_'+phase] for phase in content.PHASES if row['bytes_'+phase] is not None}
            if key == 'rec_preparation_basis' and row['has_modes']:
                value['modes'] = [row[field] for field in content.MODES]
        return json.dumps(value)
    if key == 'rec_group_shadow_progress':
        return json.dumps({'phase':row['phase'], 'at':row['reported_at'], 'reason':row['reason']}) if row['phase'] else default
    return json.dumps({'generation':row['recorded_generation'], 'serial':row['input_serial'],
                       'at':row['checkpoint_at']}) if row['checkpoint_at'] is not None else default


def fallback(conn, key, default):
    row = conn.execute('SELECT value FROM main.settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def retained_write(conn, values):
    for key, value in values.items():
        if value is None:
            conn.execute('DELETE FROM main.settings WHERE key=?', (key,))
        else:
            conn.execute('INSERT INTO main.settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (key, value))
