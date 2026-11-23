"""Fold preparation checkpoints and finish retirement of live recommendation trials."""
import json

from .. import policy_documents, recommendation_outputs, recommendation_setting_outputs
from ..schema_contract import MigrationError
from .home_display_predecessor import release_group_context

NAME = 'compact_model_selection_storage'
CHECKPOINT = 'content_full_checkpoint'
CHECKPOINTS = ('basis', 'fit')
TRIAL_TABLES = (
    'model_monitor_baseline', 'model_listening_monitor', 'model_comparison_plan',
    'model_album_assignment', 'model_pair_heads', 'model_pair_contexts', 'model_pair_files',
    'model_pair_group_roots', 'model_pair_modes', 'model_pair_policies',
    'model_pair_recommendation_roots', 'model_pair_publications',
)
RETIRED = (*TRIAL_TABLES, 'content_basis_checkpoint', 'content_fit_checkpoint', 'model_selection_schedule')
OWNERS = (*RETIRED, CHECKPOINT, 'model_selection_heads')

INTEGER = "INTEGER CHECK({0} IS NULL OR (typeof({0})='integer' AND {0}>=0))"
REAL = "REAL CHECK({0} IS NULL OR (typeof({0}) IN ('integer','real') AND abs({0})<=1.7976931348623157e308))"
FLAG = 'INTEGER NOT NULL DEFAULT 0 CHECK({0} IN (0,1))'
PHASES = {f'bytes_{phase}': INTEGER for phase in ('encoding', 'clustering', 'publication')}
COLUMNS = {
    'basis': dict(serial=INTEGER, rows=INTEGER, at=REAL, reason='TEXT', estimated_bytes=INTEGER,
                  dimensions=INTEGER, input_fingerprint='TEXT', has_modes=FLAG,
                  mode_count=INTEGER.replace('>=0', '>0'), mode_min_members=INTEGER.replace('>=0', '>0'),
                  mode_match_threshold='REAL CHECK({0} IS NULL OR {0} BETWEEN 0 AND 1)',
                  has_phases=FLAG, **PHASES),
    'fit': dict(state="TEXT CHECK({0} IN ('ready','waiting','deferred'))", reason='TEXT', attempted_at=REAL,
                attempts=INTEGER, since=REAL, next_attempt_at=REAL, memory_bytes=INTEGER,
                estimated_bytes=INTEGER, rows=INTEGER, dimensions=INTEGER, max_rows=INTEGER,
                has_phases=FLAG, **PHASES),
}


def predecessor_objects(conn):
    return [sql for (sql,) in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE tbl_name IN ({",".join("?" for _ in OWNERS)}) '
        'AND sql IS NOT NULL ORDER BY type,name', OWNERS)]


def _retire_radio_assignment(conn):
    key = 'radio_seed_offer'
    row = conn.execute('SELECT filename,content_digest,byte_size FROM radio_seed_output WHERE id=1').fetchone()
    root = recommendation_outputs.directory(conn)
    with policy_documents.retained_reader(root):
        try:
            raw = recommendation_outputs.read(*row, surface=key, chunk_key='') if row else None
        except (OSError, ValueError):
            raw = None
    if raw is None:
        conn.execute('UPDATE radio_seed_output SET run_ref=NULL,comparison_run_id=NULL,run_resolution=NULL '
                     'WHERE comparison_run_id IS NOT NULL')
        return
    value = recommendation_outputs.decode_json(raw)
    if 'comparison' not in value:
        return
    del value['comparison']
    _, _, encoded = recommendation_setting_outputs.prepare(key, json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    with policy_documents.retained_reader(root):
        reference = recommendation_outputs.publish(root, encoded)
        conn.execute('UPDATE radio_seed_output SET run_ref=NULL,comparison_run_id=NULL,run_resolution=NULL,'
                     'filename=?,content_digest=?,byte_size=? WHERE id=1', reference)


def _retire_trials(conn):
    from .. import home_outputs, json_cache
    from . import home_display_predecessor

    conn.execute("UPDATE recommendation_generation_surfaces SET selected=0 "
                 "WHERE selected=1 AND (surface GLOB 'model_pair:*' OR "
                 'generation_id IN (SELECT generation_id FROM model_pair_publications))')
    conn.execute('DELETE FROM model_pair_heads')
    conn.execute("UPDATE model_pair_publications SET status='retiring' WHERE status<>'retiring'")
    for name in TRIAL_TABLES:
        conn.execute(f'DROP TABLE {name}')
    for (key,) in conn.execute("SELECT external_key FROM group_evidence_owners WHERE kind='comparison'").fetchall():
        release_group_context(conn, 'comparison', key)
    conn.execute("DELETE FROM recommendation_generation_refs WHERE kind='comparison'")
    old_report = conn.execute("UPDATE recommendation_report_publications SET selected=0 "
                             "WHERE selected=1 AND surface='model_live_report' RETURNING id").fetchone()
    if old_report:
        conn.execute("UPDATE recommendation_report_publications SET status='retiring' WHERE id=?", old_report)
        conn.execute('DELETE FROM choice_format_usage WHERE publication_id=?', old_report)
        conn.execute('DELETE FROM recommendation_report_files WHERE publication_id=?', old_report)
        conn.execute('DELETE FROM recommendation_report_publications WHERE id=?', old_report)
    conn.execute("DELETE FROM recommendation_file_writes WHERE output_owner='model_pair_files' "
                 "OR surface='model_live_report'")
    _retire_radio_assignment(conn)
    conn.execute("DELETE FROM rec_proposals WHERE surface GLOB 'model_pair:*' OR surface IN "
                 "('model_live_comparison','model_listening_monitor','model_album_assignment','model_live_report')")
    try:
        raw = home_display_predecessor.encoded(conn, 'home_choice')
    except (OSError, ValueError):
        raw = None
    cached = conn.execute("SELECT payload FROM rec_proposals WHERE surface='home_choice'").fetchone()
    for value, clear in ((raw, lambda: home_display_predecessor.clear(conn, 'home_choice')),
                         (cached[0] if cached else None, lambda: conn.execute(
                             "DELETE FROM rec_proposals WHERE surface='home_choice'"))):
        if value is None:
            continue
        try:
            menu = json_cache.decode(value, keys={'choice_context'}, max_bytes=home_outputs.MAX_DECODED_BYTES)
        except (TypeError, ValueError):
            continue
        context = menu.get('choice_context') if isinstance(menu, dict) else None
        if isinstance(context, dict) and context.get('comparison'):
            clear()
    inactive = "SELECT id FROM rec_model_selection_runs WHERE json_extract(result_json,'$.kind')='product'"
    for column in ('active_run_id', 'display_run_id', 'restore_run_id'):
        conn.execute(f'UPDATE model_selection_heads SET {column}=NULL WHERE {column} IN ({inactive})')
    conn.execute('ALTER TABLE model_selection_heads DROP COLUMN live_run_id')
    conn.execute("DELETE FROM settings WHERE key IN ('rec_selection_live_run','retired_recommendation_trials') "
                 "OR (key IN ('rec_selection_active_run','rec_selection_display_run','rec_selection_restore') "
                 f'AND CAST(value AS INTEGER) IN ({inactive}))')
    for name in ('rec_model_previous_vectors', 'rec_model_forecasts'):
        conn.execute(f'DELETE FROM {name} WHERE run_id IN ({inactive})')
    conn.execute(f'DELETE FROM rec_model_selection_runs WHERE id IN ({inactive})')


def install(conn, _context=None):
    _retire_trials(conn)
    for phase, fields in COLUMNS.items():
        conn.execute(f'ALTER TABLE {CHECKPOINT} ADD COLUMN has_{phase} {FLAG.format(f"has_{phase}")}')
        for field, declaration in fields.items():
            column = f'{phase}_{field}'
            conn.execute(f'ALTER TABLE {CHECKPOINT} ADD COLUMN {column} {declaration.format(column)}')
        source = f'content_{phase}_checkpoint'
        assignments = ','.join(f'{phase}_{field}=p.{field}' for field in fields)
        conn.execute(f'UPDATE {CHECKPOINT} SET has_{phase}=1,{assignments} FROM {source} AS p WHERE p.singleton=1')
        conn.execute(f'DROP TABLE {source}')
    schedule = conn.execute('SELECT next_at,updated_at FROM model_selection_schedule WHERE id=1').fetchone()
    if schedule is not None:
        conn.execute("INSERT INTO settings(key,value) VALUES ('daily_model_schedule',?) "
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (json.dumps(tuple(schedule)),))
    conn.execute('DROP TABLE model_selection_schedule')


def validate(conn):
    if conn.execute(f'SELECT 1 FROM sqlite_schema WHERE name IN ({",".join("?" for _ in RETIRED)})', RETIRED).fetchone():
        raise MigrationError('Retired model selection storage remains')
