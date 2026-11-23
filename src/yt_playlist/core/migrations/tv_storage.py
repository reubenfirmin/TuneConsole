"""Consolidate TV state within its existing preparation and playback owners."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from ..tv_launch_actions import FIELDS


NAME = 'consolidate_tv_storage'
CLOCKS = {'artwork': 'tv_artwork_state', 'artwork_requests': 'tv_artwork_request_state',
          'visual': 'tv_visual_state'}
POOL_FIELDS = ('built_at', 'field_mask', 'schema_version', 'cache_key', 'at', 'offset', 'state', 'retry_at')
RETIRED = ('tv_pool_state', 'tv_pool_attempts', *CLOCKS.values(), 'tv_artist_images',
           'tv_visual_samples', 'tv_launch_videos', 'tv_append_items')
OWNERS = ('tv_preparation_state', 'tv_pool_items', 'tv_artist_artwork', 'tv_visual_results',
          'tv_launches', 'tv_append_intents', *RETIRED)
VIDEO_FIELDS = ('field_mask', 'video_id', 'title', 'artist', 'thumbnail', 'duration', 'kind', 'source_key',
                'source_artist', 'source_group', 'connection', 'key', 'source_recording_id', 'source_resolution')


def predecessor_objects(conn):
    placeholders = ','.join('?' for _ in OWNERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND tbl_name IN ({placeholders}) ORDER BY type,name', OWNERS)]


def _time(column):
    return (f'{column} REAL CHECK({column} IS NULL OR (typeof({column}) IN (\'integer\',\'real\') '
            f'AND abs({column})<=1.7976931348623157e308))')


def _preparation(conn):
    columns = (_time('pool_built_at'), 'pool_field_mask INTEGER CHECK(pool_field_mask BETWEEN 0 AND 255)',
               'pool_schema_version INTEGER CHECK(pool_schema_version>=0)', 'pool_cache_key TEXT', _time('pool_at'),
               'pool_offset INTEGER CHECK(pool_offset>=0)',
               "pool_state TEXT CHECK(pool_state IN ('searching','exhausted','no_sources'))", _time('pool_retry_at'))
    for prefix in CLOCKS:
        conn.execute(f'ALTER TABLE tv_preparation_state ADD COLUMN {prefix}_published INTEGER NOT NULL DEFAULT 0 '
                     f'CHECK({prefix}_published IN (0,1))')
        conn.execute(f'ALTER TABLE tv_preparation_state ADD COLUMN {_time(prefix + "_built_at")}')
    for declaration in columns:
        conn.execute('ALTER TABLE tv_preparation_state ADD COLUMN ' + declaration)
    if conn.execute('SELECT 1 FROM tv_pool_state').fetchone() or any(
            conn.execute('SELECT 1 FROM ' + table).fetchone() for table in CLOCKS.values()):
        conn.execute('INSERT OR IGNORE INTO tv_preparation_state(id,failed,updated_at,pool_state_present) VALUES(1,0,0,0)')
    for prefix, table in CLOCKS.items():
        conn.execute(f'UPDATE tv_preparation_state SET {prefix}_published=1,{prefix}_built_at=t.built_at '
                     f'FROM {table} t WHERE tv_preparation_state.id=t.id')
    columns = ','.join('pool_' + field for field in POOL_FIELDS)
    source = ','.join('p.' + field for field in POOL_FIELDS)
    conn.execute(f'UPDATE tv_preparation_state SET ({columns})=({source}) FROM tv_pool_state p '
                 'WHERE tv_preparation_state.id=p.id')


def _pool_items(conn):
    absent = ' AND '.join(f'{field} IS NULL' for field in VIDEO_FIELDS)
    conn.execute('ALTER TABLE tv_pool_items RENAME TO previous_tv_pool_items')
    conn.execute(f'''CREATE TABLE tv_pool_items (
        pool_id INTEGER NOT NULL REFERENCES tv_preparation_state(id),
        role TEXT NOT NULL DEFAULT 'candidate' CHECK(role IN ('candidate','attempt')),
        position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position BETWEEN 0 AND 59),
        field_mask INTEGER CHECK(typeof(field_mask)='integer' AND field_mask BETWEEN 0 AND 2047 OR field_mask IS NULL),
        video_id TEXT CHECK(length(video_id)=11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
        title TEXT, artist TEXT, thumbnail TEXT,
        duration INTEGER CHECK(duration IS NULL OR (typeof(duration)='integer' AND duration>0)),
        kind TEXT, source_key TEXT, source_artist TEXT,
        source_group TEXT CHECK(source_group IN ('liked','your_artists','discovery')),
        connection TEXT, key TEXT CHECK(key=video_id),
        source_recording_id INTEGER REFERENCES recordings(id), source_resolution TEXT,
        recording_id INTEGER REFERENCES recordings(id), resolution_reason TEXT,
        attempt_key TEXT CHECK(length(attempt_key)>0),
        CHECK((role='candidate' AND field_mask IS NOT NULL AND video_id IS NOT NULL AND attempt_key IS NULL AND
               ((source_recording_id IS NOT NULL AND source_resolution IS NULL) OR
                (source_recording_id IS NULL AND source_resolution IS 'missing_recording'))) OR
              (role='attempt' AND attempt_key IS NOT NULL AND {absent})),
        CHECK((recording_id IS NOT NULL AND resolution_reason IS NULL) OR
              (recording_id IS NULL AND resolution_reason IS CASE role WHEN 'candidate' THEN 'external_video' ELSE 'missing_recording' END)),
        PRIMARY KEY(pool_id,role,position), UNIQUE(pool_id,video_id), UNIQUE(pool_id,attempt_key)
    ) WITHOUT ROWID''')
    columns = 'pool_id,position,' + ','.join(VIDEO_FIELDS) + ',recording_id'
    conn.execute(f'INSERT INTO tv_pool_items({columns},resolution_reason) SELECT {columns},video_resolution '
                 'FROM previous_tv_pool_items')
    conn.execute("INSERT INTO tv_pool_items(pool_id,role,position,attempt_key,recording_id,resolution_reason) "
                 "SELECT pool_id,'attempt',position,raw_key,recording_id,resolution_reason FROM tv_pool_attempts")
    conn.execute('DROP TABLE previous_tv_pool_items')


def _ordered_values(conn):
    conn.execute("ALTER TABLE tv_artist_artwork ADD COLUMN image_urls TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(image_urls) IS 'array' AND json_array_length(image_urls)<=6)")
    conn.execute('UPDATE tv_artist_artwork AS a SET image_urls=(SELECT json_group_array(url ORDER BY position) '
                 'FROM tv_artist_images WHERE artwork_id=a.id)')
    for column in ('first_sample_at', 'second_sample_at'):
        conn.execute(f'ALTER TABLE tv_visual_results ADD COLUMN {column} REAL '
                     f'CHECK({column} IS NULL OR (typeof({column}) IN (\'integer\',\'real\') '
                     f'AND {column} BETWEEN 0 AND 1.7976931348623157e308))')
    conn.execute('UPDATE tv_visual_results AS r SET '
                 'first_sample_at=(SELECT at FROM tv_visual_samples WHERE result_id=r.id ORDER BY position LIMIT 1),'
                 'second_sample_at=(SELECT at FROM tv_visual_samples WHERE result_id=r.id ORDER BY position LIMIT 1 OFFSET 1)')
    conn.execute("ALTER TABLE tv_launches ADD COLUMN videos TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(videos) IS 'array' AND json_array_length(videos)<=60)")
    fields = ','.join(f"'{field}',{field}" for field in ('video_id', *FIELDS, 'write_state'))
    conn.execute(f'UPDATE tv_launches AS l SET videos=(SELECT json_group_array(json_object({fields}) ORDER BY position) '
                 'FROM tv_launch_videos WHERE request_id=l.request_id)')
    for role in ('before', 'additions', 'items'):
        column = role + '_videos'
        conn.execute(f"ALTER TABLE tv_append_intents ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]' "
                     f"CHECK(json_type({column}) IS 'array')")
        conn.execute(f'UPDATE tv_append_intents AS a SET {column}=(SELECT json_group_array(video_id_snapshot '
                     'ORDER BY position) FROM tv_append_items WHERE intent_id=a.id AND role=?)', (role,))


def install(conn, _context):
    _preparation(conn)
    _pool_items(conn)
    _ordered_values(conn)
    for table in reversed(RETIRED):
        conn.execute('DROP TABLE ' + table)


def validate(conn):
    installed = definitions(conn, OWNERS)
    if set(installed) != set(OWNERS) - set(RETIRED):
        raise MigrationError('Incomplete TV storage consolidation')
