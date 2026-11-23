"""Keep prediction provenance and model identity with the prediction."""
import re

from .. import vibe_provenance
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'fold_prediction_provenance'
RECEIPT_LOOKUP = 'index_prediction_receipt_references'
RECEIPT_INDEX = 'ix_predictions_provenance_receipt'
_RECEIPT_INDEX_TARGET = (f'{RECEIPT_INDEX} ON predictions(provenance_file_id) '
                         'WHERE provenance_file_id IS NOT NULL')
TABLE = 'predictions'
STAGE = '_predictions_preparation'
CURSOR = 'migration:predictions:cursor'
HIGH_WATER = vibe_provenance.HIGH_WATER
PAGE_SIZE = 512
RETIRED = (
    'prediction_artists', 'prediction_inputs', 'prediction_id_state',
    'prediction_model_generations', 'prediction_provenance',
)
_FACTS = ('id', 'recording_id', 'vibe', 'score', 'support', 'subgenre_key', 'kind',
          'lineage_complete', 'policy_version_id', 'predicted_at')
_EXTRA = (
    ('model_generation', "BLOB CHECK(model_generation IS NULL OR "
     "(typeof(model_generation)='blob' AND length(model_generation)=32))"),
    ('provenance_fingerprint', 'BLOB'),
    ('provenance_bytes', 'INTEGER'),
    ('provenance_file_id', 'INTEGER REFERENCES provenance_file_writes(id)'),
    ('provenance_state', "TEXT CHECK((provenance_state IS NULL AND "
     "provenance_fingerprint IS NULL AND provenance_bytes IS NULL AND provenance_file_id IS NULL) OR "
     "(provenance_state IS NOT NULL AND provenance_state IN ('ready','expired') AND "
     "provenance_fingerprint IS NOT NULL AND length(provenance_fingerprint)=32 AND "
     "provenance_bytes IS NOT NULL AND provenance_bytes>0))"),
)
_INDEXES = {
    'ix_predictions_recording_vibe': 'ON {table}(recording_id,vibe,id)',
    'ix_predictions_model_generation': 'ON {table}(model_generation,id) WHERE model_generation IS NOT NULL',
    'ix_predictions_provenance_file': "ON {table}(provenance_file_id) WHERE provenance_state='ready'",
}
MARKER = 'predictions_provenance_immutable'
INDEXES = frozenset(_INDEXES)
_FACT_COLUMNS = ','.join(_FACTS)
_COPY_COLUMNS = _FACT_COLUMNS + ',' + ','.join(name for name, _ in _EXTRA)
_PROJECTION = (
    f'INSERT OR REPLACE INTO {STAGE}({_COPY_COLUMNS}) SELECT '
    + ','.join('p.' + name for name in _FACTS)
    + ',g.generation,f.input_fingerprint,f.byte_size,f.file_id,f.state '
    'FROM predictions p LEFT JOIN prediction_model_generations g ON g.prediction_id=p.id '
    'LEFT JOIN prediction_provenance f ON f.prediction_id=p.id '
)


def install_receipt_lookup(conn, _context):
    # Foreign-key checks look up all referencing rows, including expired provenance.
    conn.execute('CREATE INDEX IF NOT EXISTS ' + _RECEIPT_INDEX_TARGET)
    validate_receipt_lookup(conn)


def validate_receipt_lookup(conn):
    actual = definitions(conn, (RECEIPT_INDEX,))
    if tokens(actual.get(RECEIPT_INDEX)) != tokens('CREATE INDEX ' + _RECEIPT_INDEX_TARGET):
        raise MigrationError('Incompatible prediction receipt index')


def predecessor_objects(conn):
    owners = (TABLE, *RETIRED)
    marks = ','.join('?' for _ in owners)
    return [sql for sql, in conn.execute(
        f"SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND (tbl_name IN ({marks}) "
        "OR name IN ('recording_vibes_insert','recording_vibes_update')) ORDER BY type,name", owners)]


def _create(conn):
    old = definitions(conn, (TABLE,))[TABLE]
    conn.execute(re.sub(r'^CREATE TABLE "?predictions"?', 'CREATE TABLE ' + STAGE, old))
    for name, declaration in _EXTRA:
        conn.execute(f'ALTER TABLE {STAGE} ADD COLUMN {name} {declaration}')
    for name, clause in _INDEXES.items():
        conn.execute(f'CREATE INDEX {name} ' + clause.format(table=STAGE))
    conn.execute('INSERT INTO settings(key,value) VALUES (?,NULL)', (CURSOR,))


def _scan_page(conn, after):
    where = 'WHERE id>?' if after is not None else ''
    ids = [row[0] for row in conn.execute(f'SELECT id FROM predictions {where} ORDER BY id LIMIT {PAGE_SIZE}',
                                         () if after is None else (after,))]
    if ids:
        conn.execute(_PROJECTION + 'WHERE p.id BETWEEN ? AND ?', (ids[0], ids[-1]))
    conn.execute('UPDATE settings SET value=? WHERE key=?', (ids[-1] if ids else 'done', CURSOR))


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if vibe_provenance.has_sql_links(conn) and vibe_provenance.convert_sql_links(conn):
        return True
    row = conn.execute('SELECT value FROM settings WHERE key=?', (CURSOR,)).fetchone()
    if row is None:
        _create(conn)
        return True
    # Older versions could leave edits to records already copied. Rebuild
    # only that scratch copy from the authoritative predecessor.
    if conn.execute("DELETE FROM settings WHERE key GLOB 'migration:predictions:changed:*'").rowcount:
        conn.execute(f'DELETE FROM {STAGE}')
        conn.execute('UPDATE settings SET value=NULL WHERE key=?', (CURSOR,))
        return True
    if row[0] != 'done':
        _scan_page(conn, row[0])
        return True
    return False


def prepare(conn, version):
    more = True
    while more:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            more = _prepare_page(conn, version)


def _check_prepared(conn):
    cursor = conn.execute('SELECT value FROM settings WHERE key=?', (CURSOR,)).fetchone()
    if cursor is None and conn.execute('SELECT 1 FROM predictions LIMIT 1').fetchone() is None:
        _create(conn)
        return
    if cursor is None or cursor[0] != 'done' or vibe_provenance.has_sql_links(conn):
        raise PreparationChanged('Prediction provenance preparation is incomplete')


def _guards():
    for action in ('insert', 'update'):
        yield f'recording_vibes_{action}', vibe_provenance.current_fit_guard(
            action, "p.lineage_complete=0 OR p.provenance_state='ready'")
    yield 'predictions_immutable', (
        f'CREATE TRIGGER predictions_immutable BEFORE UPDATE OF {_FACT_COLUMNS} ON predictions '
        "BEGIN SELECT RAISE(ABORT,'Vibe calculations are immutable'); END")
    yield 'predictions_provenance_immutable', (
        'CREATE TRIGGER predictions_provenance_immutable BEFORE UPDATE ON predictions WHEN '
        '(OLD.model_generation IS NOT NULL AND OLD.model_generation IS NOT NEW.model_generation) OR '
        '(OLD.provenance_state IS NOT NULL AND (NEW.provenance_state IS NULL OR '
        'OLD.provenance_fingerprint IS NOT NEW.provenance_fingerprint OR '
        'OLD.provenance_bytes IS NOT NEW.provenance_bytes OR '
        'OLD.provenance_file_id IS NOT NEW.provenance_file_id)) '
        "BEGIN SELECT RAISE(ABORT,'Calculation provenance is immutable'); END")
    yield 'predictions_provenance_expiry', (
        'CREATE TRIGGER predictions_provenance_expiry BEFORE UPDATE OF provenance_state ON predictions '
        "WHEN NEW.provenance_state='expired' AND EXISTS(SELECT 1 FROM recording_vibes WHERE prediction_id=OLD.id) "
        "BEGIN SELECT RAISE(ABORT,'Current provenance is required'); END")
    yield 'prediction_id_insert', (
        'CREATE TRIGGER prediction_id_insert AFTER INSERT ON predictions BEGIN '
        f"UPDATE settings SET value=max(CAST(value AS INTEGER),NEW.id) WHERE key='{HIGH_WATER}'; END")
    yield 'prediction_id_monotonic', (
        'CREATE TRIGGER prediction_id_monotonic BEFORE UPDATE ON settings '
        f"WHEN OLD.key='{HIGH_WATER}' AND (NEW.key<>OLD.key OR NEW.value IS NULL "
        "OR CAST(NEW.value AS INTEGER)<CAST(OLD.value AS INTEGER)) "
        "BEGIN SELECT RAISE(ABORT,'Calculation allocation cannot go backwards'); END")


def install(conn, _context):
    _check_prepared(conn)
    conn.execute('INSERT INTO settings(key,value) SELECT ?,high_water FROM prediction_id_state WHERE id=1',
                 (HIGH_WATER,))
    for action in ('insert', 'update'):
        conn.execute('DROP TRIGGER recording_vibes_' + action)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    conn.execute('DROP TABLE predictions')
    conn.execute(f'ALTER TABLE {STAGE} RENAME TO predictions')
    for _, sql in _guards():
        conn.execute(sql)
    conn.execute('DELETE FROM settings WHERE key=?', (CURSOR,))


def validate(conn):
    expected = dict(_guards())
    actual = definitions(conn, (*expected, *RETIRED, MARKER, TABLE))
    native_sequence = 'autoincrement' in tokens(actual.get(TABLE))
    if native_sequence:
        for name in ('prediction_id_insert', 'prediction_id_monotonic'):
            expected.pop(name)
            if name in actual:
                raise MigrationError('Obsolete prediction ID trigger: ' + name)
    if (MARKER not in actual or set(RETIRED) & actual.keys()
            or any(tokens(actual.get(name)) != tokens(sql) for name, sql in expected.items())
            or (not native_sequence and not conn.execute('SELECT 1 FROM settings WHERE key=?', (HIGH_WATER,)).fetchone())):
        raise MigrationError('Incompatible folded prediction provenance')
