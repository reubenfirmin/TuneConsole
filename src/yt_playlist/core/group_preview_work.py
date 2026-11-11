"""Durable preview pages invalidated by writes to their consumed inputs.

Page deltas avoid rewriting the growing artist/album prefix at every boundary.
Invalidation changes one flag; the worker replaces obsolete scratch work.
"""
import json

from .db_revisions import (_KEY_COLUMNS, _KEY_COLUMN_EXCLUSIONS,
                          _COLUMN_GROUP_EXCLUSIONS, _COLUMN_GROUP_INCLUSIONS,
                          _identifier, _literal)
from .schema_contract import MigrationError, tokens

TABLES = frozenset(('group_preview_work', 'group_preview_dependencies', 'group_preview_pages'))
SOURCES = frozenset('''accounts album_primary_provider_ids album_provider_ids artist_provider_ids
    catalog_entries discovery_memberships discovery_origin_tokens discovery_origins discovery_sources
    entry_artists entry_field_choices generation_groups genre_catalog genre_redirects group_generations
    group_head group_members group_publications group_version_aliases group_versions identities item_ratings
    library_memberships listening_day_counts listening_observation_subjects listening_observations
    metadata_choice_heads metadata_observations metadata_runtime_choices metadata_runtime_state
    metadata_runtime_genres metadata_runtime_providers
    metadata_runtime_recording_sources metadata_runtime_entry_sources metadata_runtime_recording_genres
    native_metadata_observations metadata_deliveries published_metadata_head published_metadata_destination_head
    published_metadata_destination_native published_metadata_destination_deliveries
    play_event_calendar play_event_sources play_event_subjects play_events player_events playlist_items playlists
    playlist_provenance prepared_outputs rec_feedback rec_proposals rec_weights rec_musical_profiles
    recommendation_candidates recommendation_generation_surfaces recommendation_generations recommendation_heads
    recommendation_policies recommendation_policy_bodies recommendation_pool_revalidations recommendation_pools
    recommendation_surface_pools recordings settings taste_axes taste_groups track_artists unresolved_items
    unresolved_playlist_items youtube_items choice_learning_heads choice_learning_publications
    choice_learning_parameters choice_learning_exclusions choice_group_estimates choice_group_opportunities'''.split())


def definitions(conn):
    yield 'group_preview_work', '''CREATE TABLE group_preview_work (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),token TEXT NOT NULL,
        schema_version INTEGER NOT NULL,started_at REAL NOT NULL,expires_at REAL,
        invalid INTEGER NOT NULL DEFAULT 0 CHECK(invalid IN (0,1)))'''
    yield 'group_preview_dependencies', '''CREATE TABLE group_preview_dependencies (
        table_name TEXT NOT NULL,record_key TEXT NOT NULL,
        PRIMARY KEY(table_name,record_key)) WITHOUT ROWID'''
    yield 'group_preview_pages', '''CREATE TABLE group_preview_pages (
        group_version TEXT NOT NULL,lane TEXT NOT NULL,after INTEGER NOT NULL,
        delta_json TEXT NOT NULL,PRIMARY KEY(group_version,lane,after)) WITHOUT ROWID'''
    yield from guards(conn)


def guards(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
    for table in sorted(SOURCES & tables):
        columns = [row[1] for row in conn.execute(f'PRAGMA main.table_info({_identifier(table)})')]
        groups = {group: [col for col in columns if col not in excluded]
                  for group, excluded in _COLUMN_GROUP_EXCLUSIONS.get(table, {}).items()}
        groups.update({group: [col for col in columns if col in included]
                       for group, included in _COLUMN_GROUP_INCLUSIONS.get(table, {}).items()})
        keys = _KEY_COLUMNS.get(table, ())
        keys = (keys,) if isinstance(keys, str) else keys
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            changed = lambda selected: ' OR '.join(
                f'OLD.{_identifier(col)} IS NOT NEW.{_identifier(col)}' for col in selected)
            matches = ["record_key=''"]
            sides = ('OLD', 'NEW') if event == 'UPDATE' else ('OLD',) if event == 'DELETE' else ('NEW',)
            keyed_change = changed([col for col in columns if col not in _KEY_COLUMN_EXCLUSIONS.get(table, ())])
            for key in keys:
                if key in columns:
                    for side in sides:
                        condition = f"record_key='key:'||{side}.{_identifier(key)}"
                        if event == 'UPDATE':
                            condition = f'({condition} AND ({keyed_change}))'
                        matches.append(condition)
            for group, selected in groups.items():
                condition = 'record_key=' + _literal('group:' + group)
                if event == 'UPDATE':
                    condition = f'({condition} AND ({changed(selected)}))'
                matches.append(condition)
            when = f' WHEN {changed(columns)}' if event == 'UPDATE' else ''
            name = f'group_preview_changed_{table}_{event.lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {event} ON main.{_identifier(table)}{when} BEGIN '
                'UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0 AND EXISTS('
                'SELECT 1 FROM group_preview_dependencies WHERE table_name=' + _literal(table) +
                ' AND (' + ' OR '.join(matches) + ')); END')


def install(conn, context=None):
    for _, sql in definitions(conn):
        conn.execute(sql)


def install_guards(conn):
    for name, sql in guards(conn):
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def validate(conn):
    from .schema_inspection import definitions as read_definitions
    expected = dict(definitions(conn))
    actual = read_definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible adaptive preview checkpoint: ' + name)


def read(conn):
    row = conn.execute('SELECT token,schema_version,started_at,expires_at,invalid '
                       'FROM group_preview_work WHERE singleton=1').fetchone()
    if row is None:
        return None
    state = dict(zip(('token', 'schema_version', 'at', 'expires_at', 'invalid'), row))
    if not state['invalid']:
        state['dependencies'] = list(map(tuple, conn.execute(
            'SELECT table_name,record_key FROM group_preview_dependencies')))
        state['pages'] = [(version, lane, json.loads(delta)) for version, lane, delta in conn.execute(
            'SELECT group_version,lane,delta_json FROM group_preview_pages ORDER BY group_version,lane,after')]
    return state


def save(conn, header, expected_token, page, dependencies):
    """The caller validates the original reader under its writer reservation."""
    row = conn.execute('SELECT token,invalid FROM group_preview_work WHERE singleton=1').fetchone()
    if (row[0] if row else None) != expected_token:
        return False
    if row is not None and row[0] == header['token']:
        if row[1]:
            return False
    else:
        for table in TABLES:
            conn.execute('DELETE FROM ' + table)
        conn.execute('INSERT INTO group_preview_work(singleton,token,schema_version,started_at,expires_at) '
                     'VALUES (1,?,?,?,?)',
                     (header['token'], header['schema_version'], header['at'], header['expires_at']))
    conn.execute('UPDATE group_preview_work SET expires_at=? WHERE singleton=1', (header['expires_at'],))
    version, lane, after, delta = page
    conn.execute('INSERT INTO group_preview_pages VALUES (?,?,?,?)',
                 (version, lane, after, json.dumps(delta, separators=(',', ':'))))
    conn.executemany('INSERT OR IGNORE INTO group_preview_dependencies VALUES (?,?)', sorted(dependencies))
    return True
