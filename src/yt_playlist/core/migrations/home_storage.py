"""Fold the current Home selection and its small supporting tables."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'fold_home_storage'
MARKER = 'uq_home_current_publication'
RETIRED = (
    'home_display_heads', 'home_genre_request_items', 'clicked_library_genres',
    'home_offer_signatures', 'home_offer_dedup', 'home_revisit_progress', 'home_hidden_facet',
)
OWNERS = ('home_display_publications', 'home_genre_requests', 'clicked_library_pools', 'taste_axes')
TRIGGERS = ('home_display_transition', 'home_genre_request_list_owner', 'clicked_library_pool_update')
ARRAY = "TEXT CHECK({column} IS NULL OR json_type({column}) IS 'array')"
LISTS = ('include_genres', 'avoid_genres')


def predecessor_objects(conn):
    objects = (*OWNERS, *RETIRED, *TRIGGERS)
    sql = definitions(conn, objects)
    return [sql[name] for name in objects]


def install(conn, _context):
    prior = definitions(conn, (*OWNERS, *TRIGGERS))
    conn.execute("ALTER TABLE home_display_publications ADD COLUMN is_current INTEGER NOT NULL DEFAULT 0 "
                 "CHECK(is_current=0 OR (is_current=1 AND status='published'))")
    conn.execute('UPDATE home_display_publications SET is_current=1 '
                 'WHERE id IN (SELECT publication_id FROM home_display_heads)')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON home_display_publications(surface) WHERE is_current=1')
    for trigger in TRIGGERS:
        conn.execute(f'DROP TRIGGER {trigger}')
    transition = prior['home_display_transition'].replace(
        'NOT EXISTS(SELECT 1 FROM home_display_heads WHERE publication_id=OLD.id)', 'OLD.is_current=0')
    conn.execute(transition)

    request = prior['home_genre_requests']
    sources = []
    for column, flag, kind in zip(LISTS, ('has_includes', 'has_avoids'), ('include', 'avoid')):
        declaration = ARRAY.format(column=column)
        start = request.index('    ' + flag)
        end = request.index(',\n', start)
        request = request[:start] + f'    {column} {declaration}' + request[end:]
        request = request.replace(flag + '=0', column + ' IS NULL')
        items = ("(SELECT json_group_array(raw_key) FROM (SELECT raw_key FROM home_genre_request_items "
                 f"WHERE request_id=original.id AND kind='{kind}' ORDER BY position))")
        sources.append((column, f'CASE WHEN {flag} THEN {items} END'))
        conn.execute(f'ALTER TABLE clicked_library_pools ADD COLUMN {column} {declaration}')
        conn.execute(f'UPDATE clicked_library_pools AS pool SET {column}='
                     "(SELECT json_group_array(raw_key) FROM (SELECT raw_key FROM clicked_library_genres "
                     f"WHERE generation_id=pool.generation_id AND kind='{kind}' ORDER BY position))")
    _replace_table(conn, 'home_genre_requests', request, expressions=dict(sources))
    conn.execute(prior['clicked_library_pool_update'])

    conn.execute('ALTER TABLE taste_axes ADD COLUMN home_hidden INTEGER NOT NULL DEFAULT 0 CHECK(home_hidden IN (0,1))')
    conn.execute('UPDATE taste_axes SET home_hidden=1 WHERE id IN (SELECT axis_id FROM home_hidden_facet)')
    conn.execute("""
        INSERT INTO settings(key,value)
        SELECT 'home_offer_window',json_object('epoch',epoch,'built_at',built_at,
            'signatures',json((SELECT json_group_array(signature) FROM (
                SELECT signature FROM home_offer_signatures WHERE owner_id=1 ORDER BY signature))))
        FROM home_offer_dedup
    """)
    conn.execute("INSERT INTO settings(key,value) SELECT 'home_revisit_remaining',CAST(remaining AS TEXT) "
                 "FROM home_revisit_progress")
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn, *, display_settings=False):
    if set(definitions(conn, (*RETIRED, MARKER))) != (set() if display_settings else {MARKER}):
        raise MigrationError('Home storage was not folded')
