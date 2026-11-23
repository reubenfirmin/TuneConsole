"""Keep current Radio and TV preparation on existing owners."""
import json

from .. import recommendation_outputs as files, recommendation_work as work
from .. import radio_policy_roots, tv_artwork_state as artwork, tv_recommendation_state as tv, worker_settings
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_radio_seed_and_tv_preparation'
RETIRED = ('radio_seed_output', 'tv_preparation_state', 'tv_artist_artwork')
PREVIOUS_GUARDS = ('radio_seed_retains_entry_delete', 'radio_seed_retains_entry_rekey',
                   'retained_radio_policy_delete', 'retained_radio_policy_update')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema '
        'WHERE (tbl_name IN (?,?,?) OR name IN (?,?,?,?,?)) AND sql IS NOT NULL ORDER BY type,name',
        (*RETIRED, *PREVIOUS_GUARDS, 'artists'))]


def _copy_seed(conn):
    row = conn.execute('SELECT * FROM radio_seed_output WHERE id=1').fetchone()
    if row is None:
        return
    document = dict(row)
    for field in ('id', 'surface', 'chunk_key'):
        document.pop(field)
    for field in ('policy_roots', 'seed_subjects'):
        document[field] = json.loads(document[field])
    worker_settings.save(conn, files.RADIO_SEED_SETTING, json.dumps(document))


def _copy_tv(conn):
    row = conn.execute('SELECT * FROM tv_preparation_state WHERE id=1').fetchone()
    _copy_artwork(conn, row)
    if row is None:
        return
    if row['pool_state_present']:
        outcome = {'failed': bool(row['failed'])}
        if row['observed_at'] is not None:
            outcome['at'] = row['observed_at']
        work.write(conn, 'tv_preparation', outcome, row['updated_at'])
    if row['pool_field_mask'] is not None:
        values = (row['pool_schema_version'], row['pool_cache_key'], row['pool_at'],
                  json.loads(row['pool_candidates']), json.loads(row['pool_attempts']), row['pool_offset'],
                  row['pool_state'], row['pool_retry_at'])
        tv.save_pool(conn, dict(tv._view(row['pool_field_mask'], tv.POOL, values), built_at=row['pool_built_at']))
    if row['visual_published']:
        tv._write_visuals(conn, json.loads(row['visual_results']), row['visual_built_at'])
    worker_settings.save(conn, work.TV_DRAW_KEY, json.dumps(dict(
        number=row['draw_number'], recent=work.video_history(row['recent_videos'], work.TV_POOL_LIMIT),
        last=work.video_history(row['last_videos'], work.TV_QUEUE_LIMIT))))


def _copy_artwork(conn, row):
    state = artwork.load(conn)
    if row is not None and row['artwork_published']:
        state['published_at'] = row['artwork_built_at']
    if row is not None and row['artwork_requests_published']:
        state['requested_at'] = row['artwork_requests_built_at']
    for key, artist, mask, images, retry, at, revision in conn.execute(
            'SELECT artist_key,artist_id,field_mask,image_urls,retry_at,at,revision FROM tv_artist_artwork '
            'WHERE result_order IS NOT NULL ORDER BY result_order'):
        value = tv._view(mask, tv.ARTWORK, (json.loads(images), retry, at))
        artwork.add_result(conn, state, key, artist, value)
        state['revisions'][key] = revision
    for key, name, artist, at, revision in conn.execute(
            'SELECT artist_key,display_name,requested_artist_id,requested_at,revision FROM tv_artist_artwork '
            'WHERE demand_order IS NOT NULL ORDER BY demand_order'):
        state['requests'][key] = {'name': name, 'artist_id': artist, 'at': at}
        state['revisions'][key] = revision
    if state['results'] or state['requests'] or 'published_at' in state or 'requested_at' in state:
        artwork.save(conn, state)


def _guards():
    seed = files.RADIO_SEED_SETTING
    guards = {
        'radio_seed_retains_entry': ('track_sources', 'id',
            f"EXISTS(SELECT 1 FROM settings s,json_each(s.value,'$.seed_subjects') j "
            f"WHERE s.key='{seed}' AND j.value->>'entry_id'=OLD.id)", 'A saved Radio seed retains this entry', 'rekey'),
        'radio_seed_retains_run': ('rec_model_selection_runs', 'id',
            f"EXISTS(SELECT 1 FROM settings WHERE key='{seed}' AND value->>'run_ref'=OLD.id)",
            'A saved Radio seed retains this comparison run', 'rekey'),
        'retained_radio_policy': ('recommendation_policies', 'version', f'OLD.version IN ({radio_policy_roots.VERSIONS})',
                                  'On Air still references this policy', 'update'),
        'tv_artwork_retains_artist': ('artists', 'id',
            f"OLD.tv_artwork IS NOT NULL OR EXISTS(SELECT 1 FROM settings s,json_each(s.value,'$.requests') j "
            f"WHERE s.key='{artwork.KEY}' AND j.value->>'artist_id'=OLD.id)", 'TV retains artist artwork', 'rekey'),
    }
    for prefix, (table, key, condition, message, suffix) in guards.items():
        for action, event, changed in (('delete', 'DELETE', ''),
                                        (suffix, 'UPDATE OF ' + key, f'NEW.{key} IS NOT OLD.{key} AND ')):
            name = f'{prefix}_{action}'
            yield name, (f'CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {changed}({condition}) '
                         f"BEGIN SELECT RAISE(ABORT,'{message}'); END")


def install(conn, _context):
    conn.execute('ALTER TABLE artists ADD COLUMN ' + artwork.COLUMN)
    _copy_seed(conn)
    _copy_tv(conn)
    for name in PREVIOUS_GUARDS:
        conn.execute(f'DROP TRIGGER {name}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    for _, sql in _guards():
        conn.execute(sql)


def validate(conn):
    guards = dict(_guards())
    installed = definitions(conn, (*RETIRED, 'artists', *guards))
    if (any(table in installed for table in RETIRED)
            or artwork.COLUMN not in installed.get('artists', '')
            or any(tokens(installed.get(name)) != tokens(sql) for name, sql in guards.items())):
        raise MigrationError('Incomplete Radio and TV state cutover')
