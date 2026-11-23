"""Keep model retention on the original measurement and retire its write view."""
from ..schema_contract import MigrationError, table_clauses, tokens
from ..schema_inspection import definitions


NAME = 'fold_measurement_storage'
ROOT_COLUMN = 'used_by_model INTEGER NOT NULL DEFAULT 0 CHECK(used_by_model IN (0,1))'
RETIRED = frozenset({'prediction_measurement_roots', 'vibe_measurements', 'vibe_measurements_insert'})
_MEASUREMENT_FIELDS = ('id', 'recording_id', 'vibe', 'score', 'support', 'source', 'model_version_id',
                       'input_digest', 'measured_at')
UPDATE_GUARD = ('CREATE TRIGGER measurements_immutable BEFORE UPDATE ON measurements WHEN '
                'OLD.used_by_model<>0 OR NEW.used_by_model<>1 OR '
                + ' OR '.join(f'NEW.{name} IS NOT OLD.{name}' for name in _MEASUREMENT_FIELDS)
                + " BEGIN SELECT RAISE(ABORT,'Vibe calculations are immutable'); END")
DELETE_GUARD = ('CREATE TRIGGER measurements_model_input_delete BEFORE DELETE ON measurements '
                "WHEN OLD.used_by_model=1 BEGIN SELECT RAISE(ABORT,'Measurement is retained for model evidence'); END")


def install(conn, _context):
    conn.execute('ALTER TABLE measurements ADD COLUMN ' + ROOT_COLUMN)
    conn.execute('DROP TRIGGER measurements_immutable')
    conn.execute('UPDATE measurements SET used_by_model=1 '
                 'WHERE id IN (SELECT measurement_id FROM prediction_measurement_roots)')
    conn.execute('DROP TABLE prediction_measurement_roots')
    conn.execute('DROP VIEW vibe_measurements')
    conn.execute(UPDATE_GUARD)
    conn.execute(DELETE_GUARD)


def validate(conn):
    guards = {'measurements_immutable': UPDATE_GUARD, 'measurements_model_input_delete': DELETE_GUARD}
    actual = definitions(conn, ('measurements', *guards, *RETIRED))
    if RETIRED.intersection(actual):
        raise MigrationError('Retired measurement storage remains')
    if tokens(ROOT_COLUMN) not in table_clauses(tokens(actual.get('measurements', ''))):
        raise MigrationError('Measurement retention flag is missing')
    if any(tokens(actual.get(name, '')) != tokens(sql) for name, sql in guards.items()):
        raise MigrationError('Incompatible measurement retention guards')
