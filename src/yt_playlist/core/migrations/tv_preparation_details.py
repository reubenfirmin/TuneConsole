"""Keep bounded TV lists and measurements with their publications."""
import json

from .. import tv_recommendation_state as tv
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'inline_tv_preparation_and_continuation_details'
RETIRED = ('tv_continuation_items', 'tv_pool_items', 'tv_artist_requests', 'tv_visual_results')
COLUMNS = {
    'tv_continuation_receipts': {'items': "TEXT NOT NULL DEFAULT '[]' CHECK(json_type(items) IS 'array' AND json_array_length(items)<=60)"},
    'tv_preparation_state': {
        'pool_candidates': "TEXT NOT NULL DEFAULT '[]' CHECK(json_type(pool_candidates) IS 'array' AND json_array_length(pool_candidates)<=60)",
        'pool_attempts': "TEXT NOT NULL DEFAULT '[]' CHECK(json_type(pool_attempts) IS 'array' AND json_array_length(pool_attempts)<=60)",
        'visual_results': "TEXT NOT NULL DEFAULT '{}' CHECK(json_type(visual_results) IS 'object')",
    },
    'tv_artist_artwork': {
        'revision': 'TEXT',
        'result_order': 'INTEGER',
        'demand_order': 'INTEGER',
        'display_name': 'TEXT',
        'requested_at': 'REAL',
        'requested_artist_id': 'INTEGER REFERENCES artists(id)',
        'request_resolution': 'TEXT',
    },
}


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?,?,?,?,?) '
        'AND sql IS NOT NULL ORDER BY type,name', (*COLUMNS, *RETIRED))]


def _items(conn, table, condition, owner, resolution):
    columns = ','.join('source_group' if field == 'group' else field for field in tv.ITEM)
    found = []
    for row in conn.execute(f'SELECT field_mask,{columns},position,source_recording_id,source_resolution,recording_id,'
                            f'{resolution} FROM {table} WHERE {condition} ORDER BY position', (owner,)):
        item = tv._view(row[0], tv.ITEM, row[1:1 + len(tv.ITEM)])
        found.append(dict(item, **dict(zip(tv.ITEM_SNAPSHOT, row[-5:], strict=True))))
    return json.dumps(found)


def install(conn, _context):
    for owner, columns in COLUMNS.items():
        for name, declaration in columns.items():
            conn.execute(f'ALTER TABLE {owner} ADD COLUMN {name} {declaration}')
    immutable = 'tv_receipt_complete_update'
    restore_guard = definitions(conn, (immutable,))[immutable]
    conn.execute(f'DROP TRIGGER {immutable}')
    for identity, in conn.execute('SELECT id FROM tv_continuation_receipts'):
        items = _items(conn, 'tv_continuation_items', 'receipt_id=?', identity, 'video_resolution')
        conn.execute('UPDATE tv_continuation_receipts SET items=? WHERE id=?', (items, identity))
    conn.execute(restore_guard)
    candidates = _items(conn, 'tv_pool_items', "pool_id=? AND role='candidate'", 1, 'resolution_reason')
    attempts = [dict(position=position, key=key, recording_id=recording, resolution_reason=reason)
                for position, key, recording, reason in conn.execute(
                    "SELECT position,attempt_key,recording_id,resolution_reason FROM tv_pool_items WHERE role='attempt' ORDER BY position")]
    visuals = {}
    for row in conn.execute('SELECT video_id,field_mask,' + ','.join(tv.VISUAL[:-1]) +
                            ',first_sample_at,second_sample_at FROM tv_visual_results ORDER BY id'):
        samples = [moment for moment in row[-2:] if moment is not None]
        visuals[row[0]] = tv._view(row[1], tv.VISUAL, (*row[2:-2], samples))
    conn.execute('UPDATE tv_preparation_state SET pool_candidates=?,pool_attempts=?,visual_results=? WHERE id=1',
                 (candidates, json.dumps(attempts), json.dumps(visuals)))
    conn.execute('UPDATE tv_artist_artwork SET result_order=id,revision=lower(hex(randomblob(16)))')
    conn.execute('''INSERT INTO tv_artist_artwork(artist_key,resolution_reason,field_mask,revision,demand_order,
        display_name,requested_at,requested_artist_id,request_resolution)
        SELECT artist_key,'missing_artist',0,lower(hex(randomblob(16))),id,display_name,requested_at,artist_id,resolution_reason
        FROM tv_artist_requests WHERE true ON CONFLICT(artist_key) DO UPDATE SET demand_order=excluded.demand_order,
        display_name=excluded.display_name,requested_at=excluded.requested_at,requested_artist_id=excluded.requested_artist_id,
        request_resolution=excluded.request_resolution''')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn, *, preparation_settings=False, playlist_recovery=False):
    owners = {'tv_continuation_receipts': COLUMNS['tv_continuation_receipts']} if preparation_settings else COLUMNS
    owners = {} if playlist_recovery else owners
    retired = (*RETIRED, 'tv_continuation_receipts') if playlist_recovery else RETIRED
    installed = definitions(conn, (*owners, *retired))
    if set(installed) != set(owners) or any(name not in installed[owner]
            for owner, columns in owners.items() for name in columns):
        raise MigrationError('Incompatible inline TV preparation or continuation storage')
