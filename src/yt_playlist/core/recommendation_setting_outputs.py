"""The saved Radio offer and historical manual report have separate file owners."""
import math

from . import home_outputs, recommendation_outputs as files, policy_documents

KEYS = frozenset({'radio_seed_offer', 'rec_autotune_result'})
TABLES = {'radio_seed_offer': 'radio_seed_output', 'rec_autotune_result': 'model_autotune_output'}
MAX_RAW_BYTES = 512 * 1024


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_seed_output'").fetchone() is not None


def prepare(key, raw):
    if key not in KEYS or not isinstance(raw, str) or len(raw.encode()) > MAX_RAW_BYTES:
        raise ValueError('Invalid saved recommendation output or byte budget')
    try:
        value = files.decode_json(raw)
    except ValueError:
        if key == 'radio_seed_offer':
            raise
        value = None  # Preserve an unreadable historical report verbatim.
    if key == 'radio_seed_offer':
        if not isinstance(value, dict) or not isinstance(value.get('tracks', []), list) or len(value.get('tracks', [])) > 1000:
            raise ValueError('Invalid saved Radio seed offer')
        if any(not isinstance(track, dict) or not isinstance(track.get('video_id'), str) or not track['video_id']
               for track in value.get('tracks', [])):
            raise ValueError('Radio seed has no exact video reference')
        home_outputs.policy_references(value)
    observed = value.get('ran_at') if key == 'rec_autotune_result' and isinstance(value, dict) else None
    if observed is not None and (type(observed) not in (int, float) or not math.isfinite(observed)):
        observed = None
    return value, observed, files.encode(key, '', raw)


def put(conn, values):
    if not conn.in_transaction:
        raise ValueError('Saved recommendation output requires its outer transaction')
    if set(values) - KEYS:
        raise ValueError('Unknown saved recommendation owner')
    prepared = {key: prepare(key, raw) if raw else None for key, raw in values.items()}
    with policy_documents.retained_reader(files.directory(conn)):
        for key, output in prepared.items():
            if key == 'radio_seed_offer':
                conn.execute('DELETE FROM radio_seed_policies')
                conn.execute('DELETE FROM radio_seed_subjects')
            conn.execute('DELETE FROM ' + TABLES[key])
            if output is None:
                continue
            value, observed, encoded = output
            filename, digest, size = files.publish(files.directory(conn), encoded)
            if key == 'radio_seed_offer':
                menu = value.get('menu_id')
                if menu is not None and (not isinstance(menu, str) or not 0 < len(menu.encode()) <= 4096):
                    raise ValueError('Invalid saved seed menu identity')
                assignment = value.get('comparison') or {}
                if not isinstance(assignment, dict):
                    raise ValueError('Invalid saved seed comparison')
                run = assignment.get('run_id')
                if run is not None and (type(run) is not int or not 0 < run < 2 ** 63):
                    raise ValueError('Invalid saved seed comparison run')
                known = run is not None and conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
                conn.execute('INSERT INTO radio_seed_output(id,menu_id,comparison_run_id,run_ref,run_resolution,filename,content_digest,byte_size) '
                             'VALUES (1,?,?,?,?,?,?,?)', (menu, run, run if known else None,
                             None if run is None or known else 'missing_run', filename, digest, size))
                for position, track in enumerate(value.get('tracks', [])):
                    row = conn.execute('SELECT id FROM catalog_entries WHERE video_id=? ORDER BY id LIMIT 1',
                                       (track['video_id'],)).fetchone()
                    conn.execute('INSERT INTO radio_seed_subjects VALUES (1,?,?,?,?)',
                                 (position, track['video_id'], row[0] if row else None,
                                  None if row else 'missing_entry'))
                for policy in sorted(home_outputs.policy_references(value)):
                    known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (policy,)).fetchone()
                    conn.execute('INSERT INTO radio_seed_policies VALUES (1,?,?,?)',
                                 (policy, policy if known else None, None if known else 'missing_policy'))
            else:
                conn.execute('INSERT INTO model_autotune_output(id,observed_at,filename,content_digest,byte_size) VALUES (1,?,?,?,?)',
                             (observed, filename, digest, size))


def get(conn, key, default=None):
    with policy_documents.retained_reader(files.directory(conn)):
        row = conn.execute('SELECT filename,content_digest,byte_size FROM ' + TABLES[key] + ' WHERE id=1').fetchone()
        if row is None:
            return default
        try:
            return files.read(*row, surface=key, chunk_key='')
        except (OSError, ValueError):
            return default


def clear(conn):
    put(conn, {key: None for key in KEYS})
