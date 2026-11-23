"""The saved Radio offer and historical manual report have separate file owners."""
import json
import math

from . import home_outputs, recommendation_outputs as files, policy_documents, radio_policy_roots, worker_settings

KEYS = frozenset({'radio_seed_offer', 'rec_autotune_result'})
OWNERS = {surface: owner for owner, (_, surface) in files.SAVED_OUTPUTS.items()}
MAX_RAW_BYTES = 512 * 1024


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
        menu = value.get('menu_id')
        if menu is not None and (not isinstance(menu, str) or not 0 < len(menu.encode()) <= 4096):
            raise ValueError('Invalid saved seed menu identity')
        assignment = value.get('comparison') or {}
        if not isinstance(assignment, dict):
            raise ValueError('Invalid saved seed comparison')
        run = assignment.get('run_id')
        if run is not None and (type(run) is not int or not 0 < run < 2 ** 63):
            raise ValueError('Invalid saved seed comparison run')
    observed = value.get('ran_at') if key == 'rec_autotune_result' and isinstance(value, dict) else None
    if observed is not None and (type(observed) not in (int, float) or not math.isfinite(observed)):
        observed = None
    return value, observed, files.encode(key, '', raw)


def prepare_record(key, raw):
    value, observed, encoded = prepare(key, raw)
    return {'value': value, 'observed': observed, 'encoded': encoded,
            'policies': home_outputs.policy_references(value) if key == 'radio_seed_offer' else ()}


def put(conn, values):
    if not conn.in_transaction:
        raise ValueError('Saved recommendation output requires its outer transaction')
    if set(values) - KEYS:
        raise ValueError('Unknown saved recommendation owner')
    prepared = {key: prepare_record(key, raw) if raw else None for key, raw in values.items()}
    with policy_documents.retained_reader(files.directory(conn)):
        durable = {key: files.publish(files.directory(conn), output['encoded'])
                   for key, output in prepared.items() if output is not None}
        register(conn, prepared, durable)


def register(conn, prepared, durable):
    """Replace the current saved file references and their captured metadata."""
    for key, output in prepared.items():
        setting = files.SAVED_OUTPUTS[OWNERS[key]][0]
        if output is None:
            worker_settings.save(conn, setting, None)
            continue
        value, observed = output['value'], output['observed']
        filename, digest, size = durable[key]
        document = dict(filename=filename, content_digest=digest, byte_size=size)
        if key == 'radio_seed_offer':
            menu = value.get('menu_id')
            run = (value.get('comparison') or {}).get('run_id')
            known = run is not None and conn.execute('SELECT 1 FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
            roots = radio_policy_roots.encode(conn, output['policies'])
            subjects = []
            for position, track in enumerate(value.get('tracks', [])):
                video = track['video_id']
                row = conn.execute('SELECT id FROM track_sources WHERE video_id=? ORDER BY id LIMIT 1', (video,)).fetchone()
                subjects.append(dict(position=position, video_id=video, entry_id=row[0] if row else None,
                                     resolution_reason=None if row else 'missing_entry'))
            document.update(menu_id=menu, comparison_run_id=run, run_ref=run if known else None,
                            run_resolution=None if run is None or known else 'missing_run',
                            policy_roots=json.loads(roots), seed_subjects=subjects)
        else:
            document['observed_at'] = observed
        worker_settings.save(conn, setting, json.dumps(document, allow_nan=False))


def reference(conn, key):
    document = files.saved_reference(conn, OWNERS[key])
    return {field: document[field] for field in ('filename', 'content_digest', 'byte_size')} if document else None


def get(conn, key, default=None):
    with policy_documents.retained_reader(files.directory(conn)):
        row = reference(conn, key)
        if row is None:
            return default
        try:
            return files.read(row['filename'], row['content_digest'], row['byte_size'], surface=key, chunk_key='')
        except (OSError, ValueError):
            return default


def clear(conn):
    register(conn, {key: None for key in KEYS}, {})
