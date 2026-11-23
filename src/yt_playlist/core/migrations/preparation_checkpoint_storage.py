"""Fold content and grouping preparation into settings and the group generation."""
import json

from .. import content_preparation_state as content, group_preparation_state as group, group_progress
from .. import ranking_checkpoints, recording_selection, worker_settings
from ..content_checkpoint_contract import CONFIG, FIELDS, KEYS, MODES, PHASES
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'fold_preparation_checkpoints'
RETIRED = ('content_full_checkpoint', 'group_preparation_state')
SEEDS = {
    content.CONTENT_REVISION: '0', content.EVIDENCE_REVISION: '0',
    content.REFRESH_REVISION: '1', content.ACKNOWLEDGED: '0',
    recording_selection.READY: '0', group.SERIAL: '0', group.ROOTS: '0',
}
INDEX = 'group_generation_worker_checkpoint'
DELETE_GUARD = 'retained_group_worker_checkpoint_delete'
RETENTION_GUARD = 'retained_group_checkpoint_publication_delete'
INDEX_SQL = (f'CREATE UNIQUE INDEX {INDEX} ON group_generations((1)) '
             'WHERE worker_checkpoint_at IS NOT NULL')
DELETE_SQL = (f'CREATE TRIGGER {DELETE_GUARD} BEFORE DELETE ON group_generations '
              'WHEN OLD.worker_checkpoint_at IS NOT NULL BEGIN '
              "SELECT RAISE(ABORT,'Grouping worker checkpoint must be retained'); END")


def _dependent_triggers(conn):
    return list(conn.execute(
        "SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' "
        'AND (instr(sql,?)>0 OR instr(sql,?)>0) ORDER BY name', RETIRED))


def predecessor_objects(conn):
    return [*definitions(conn, (*RETIRED, 'group_generations')).values(),
            *(sql for _, sql in _dependent_triggers(conn))]


def _singleton(conn, source):
    result = conn.execute(f'SELECT * FROM {source} WHERE singleton=1')
    row = result.fetchone()
    if row is None:
        raise MigrationError(f'Missing preparation checkpoint owner {source}')
    return dict(zip((column[0] for column in result.description), row, strict=True))


def _content_settings(row):
    for key, phase in KEYS.items():
        prefix = '' if phase == 'full' else phase + '_'
        present = 'has_checkpoint' if phase == 'full' else 'has_' + phase
        if not row[present]:
            continue
        value = {field: row[prefix + field] for field in FIELDS[key] if row[prefix + field] is not None}
        if phase == 'full':
            value['at'] = row['at']
            if row['has_config']:
                value['config'] = [row[name] for name in CONFIG]
        else:
            if row[prefix + 'has_phases']:
                value['phase_bytes'] = {name: row[prefix + 'bytes_' + name] for name in PHASES
                                        if row[prefix + 'bytes_' + name] is not None}
            if phase == 'basis' and row['basis_has_modes']:
                value['modes'] = [row[prefix + name] for name in MODES]
        yield content.CHECKPOINTS[key], json.dumps(value, allow_nan=False)


def install(conn, _context):
    content_row = _singleton(conn, RETIRED[0])
    group_row = _singleton(conn, RETIRED[1])
    state = dict(_content_settings(content_row))
    for key, column in {
        content.CONTENT_REVISION: 'content_changes', content.EVIDENCE_REVISION: 'evidence_changes',
        content.REFRESH_REVISION: 'refresh_revision', content.ACKNOWLEDGED: 'acknowledged_refresh_revision',
        content.FIT_VERSION: 'fit_version', recording_selection.READY: 'recording_selection_ready',
        **ranking_checkpoints.COLUMNS,
    }.items():
        if content_row[column] is not None:
            state[key] = str(content_row[column])
    state.update({group.SERIAL: str(group_row['serial']), group.ROOTS: str(group_row['roots_complete'])})
    if group_row['fit_fingerprint'] is not None:
        state[group.FIT] = group_row['fit_fingerprint']
    if group_row['phase'] is not None:
        state[group_progress.SETTING] = json.dumps(
            {'phase': group_row['phase'], 'at': group_row['reported_at'], 'reason': group_row['reason']},
            allow_nan=False)
    if group_row['unresolved_generation'] is not None:
        state[group.UNRESOLVED] = json.dumps(
            {'generation': group_row['unresolved_generation'], 'serial': group_row['input_serial'],
             'at': group_row['checkpoint_at']}, allow_nan=False)
    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', state.items())

    conn.execute('ALTER TABLE group_generations ADD COLUMN worker_input_serial INTEGER '
                 "CHECK(worker_input_serial IS NULL OR (typeof(worker_input_serial)='integer' AND worker_input_serial>=0))")
    conn.execute("""ALTER TABLE group_generations ADD COLUMN worker_checkpoint_at REAL CHECK(
        (worker_input_serial IS NULL AND worker_checkpoint_at IS NULL)
        OR (worker_input_serial IS NOT NULL AND typeof(worker_checkpoint_at)='real'
            AND abs(worker_checkpoint_at)<=1.7976931348623157e308))
    """)
    if group_row['generation_id'] is not None:
        conn.execute('UPDATE group_generations SET worker_input_serial=?,worker_checkpoint_at=? WHERE id=?',
                     (group_row['input_serial'], group_row['checkpoint_at'], group_row['generation_id']))
    conn.execute(INDEX_SQL)
    conn.execute(DELETE_SQL)

    replacements = {
        'UPDATE content_full_checkpoint SET evidence_changes=evidence_changes+1 WHERE singleton=1':
            worker_settings.increment_sql(content.EVIDENCE_REVISION),
        'UPDATE content_full_checkpoint SET refresh_revision=refresh_revision+1 WHERE singleton=1':
            worker_settings.increment_sql(content.REFRESH_REVISION),
        'UPDATE group_preparation_state SET serial=serial+1 WHERE singleton=1':
            worker_settings.increment_sql(group.SERIAL),
        'EXISTS(SELECT 1 FROM group_preparation_state WHERE generation_id=OLD.id)':
            'OLD.worker_checkpoint_at IS NOT NULL',
    }
    for name, sql in _dependent_triggers(conn):
        for previous, current in replacements.items():
            sql = sql.replace(previous, current)
        if any(table in sql for table in RETIRED):
            raise MigrationError(f'Unrecognized preparation checkpoint dependency {name}')
        conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(sql)
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    schema = definitions(conn, (*RETIRED, INDEX, DELETE_GUARD, RETENTION_GUARD))
    if (set(RETIRED).intersection(schema) or tokens(schema.get(INDEX)) != tokens(INDEX_SQL)
            or tokens(schema.get(DELETE_GUARD)) != tokens(DELETE_SQL)
            or 'worker_checkpoint_at' not in tokens(schema.get(RETENTION_GUARD))):
        raise MigrationError('Incomplete preparation checkpoint storage')
    for key in SEEDS:
        value = worker_settings.read(conn, key)
        if value is None or not value.isascii() or not value.isdecimal():
            raise MigrationError(f'Invalid preparation worker setting {key}')
