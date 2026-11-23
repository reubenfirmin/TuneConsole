"""Schema for preview scratch work used by the migrations before C175."""
from .db_revisions import (_KEY_COLUMNS, _KEY_COLUMN_EXCLUSIONS,
                          _COLUMN_GROUP_EXCLUSIONS, _COLUMN_GROUP_INCLUSIONS,
                          _identifier, _literal)
from .domain_contracts import EARLIER_DISCOVERY_OFFERS
from .schema_contract import MigrationError, tokens

INVALIDATE = ('UPDATE group_preparation_state SET preview_invalid=1 '
              'WHERE singleton=1 AND preview_token IS NOT NULL AND preview_invalid=0')
# The first checkpoint migration installs these guards. New inputs have
# their own schema change so installed preview contracts remain valid.
_INITIAL_SOURCES = frozenset('''accounts album_provider_ids artist_provider_ids
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
    unresolved_playlist_items youtube_items track_sources choice_learning_heads choice_learning_publications
    choice_learning_parameters choice_learning_exclusions choice_group_estimates choice_group_opportunities'''.split())
_CATALOG_SCOPE_SOURCES = frozenset((
    'account_albums', 'album_item_observations', 'album_occurrence_sources', 'album_track_occurrences',
    'album_tracklists', 'albums', 'recording_redirects', 'unattributed_album_memberships',
))
_LIBRARY_READINESS_SOURCES = frozenset({'recording_library_state'})
_PLAYLIST_SCOPE_SOURCES = frozenset({'playlist_scopes'})
METADATA_SELECTION_SOURCES = frozenset({'enrichment_provider_config', 'recording_metadata_selections'})
SOURCES = (_INITIAL_SOURCES | _CATALOG_SCOPE_SOURCES | _LIBRARY_READINESS_SOURCES | _PLAYLIST_SCOPE_SOURCES
           | METADATA_SELECTION_SOURCES)


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


def guards(conn, *, sources=_INITIAL_SOURCES, recommendation_selection=False):
    from .recording_metadata import COLUMNS as RECORDING_METADATA_COLUMNS

    tables = {row[0] for row in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
    invalidate = ('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0'
                  if 'group_preview_work' in tables else INVALIDATE)
    for table in sorted(SOURCES & sources & tables):
        columns = [row[1] for row in conn.execute(f'PRAGMA main.table_info({_identifier(table)})')]
        if table == 'track_sources' and 'library_origin' in columns:
            # The intermediate repair appends these columns with ALTER TABLE.
            # Keep the guard definition in the same order as the regular fold.
            columns.remove('library_origin')
            columns.remove('library_added_at')
            position = columns.index('source_ref') + 1
            columns[position:position] = ['library_origin', 'library_added_at']
        if table == 'playlist_scopes':
            columns = [column for column in columns if column != 'library_revisions']
        if table == 'accounts':
            columns = [column for column in columns if column != 'bindings_saved']
        if table == 'genre_catalog':
            columns = [column for column in columns if column not in ('enabled', 'preference_origin')]
        if table in ('albums', 'recordings'):
            # Retained Rediscover offer totals are independent of preview inputs.
            columns = [column for column in columns if column not in EARLIER_DISCOVERY_OFFERS]
        if table == 'albums':
            from .album_playback_context import COLUMNS as PLAYBACK_COLUMNS
            columns = [column for column in columns if column not in
                       {'discovery_found_at', 'merged_at', 'merge_digest', 'unattributed_saved_at', *PLAYBACK_COLUMNS}]
        if table == 'album_provider_ids':
            columns = [column for column in columns if not column.startswith(('fingerprint_', 'share_', 'note_'))]
        if table == 'group_versions':
            columns = [column for column in columns if not column.startswith('build_')]
        if table == 'taste_groups':
            columns = [column for column in columns if not column.startswith('mode_')]
        if table == 'group_generations':
            columns = [column for column in columns if not column.startswith(('build_', 'retention_'))]
        if table == 'group_version_aliases':
            columns = [column for column in columns if not column.startswith('retention_')]
        if table == 'recordings':
            # Preview preparation consumes the source catalog facts. Mix Tape
            # selection's derived columns do not change the saved page inputs.
            columns = [column for column in columns if column not in RECORDING_METADATA_COLUMNS]
        groups = {group: [col for col in columns if col not in excluded]
                  for group, excluded in _COLUMN_GROUP_EXCLUSIONS.get(table, {}).items()}
        groups.update({group: [col for col in columns if col in included]
                       for group, included in _COLUMN_GROUP_INCLUSIONS.get(table, {}).items()
                       if included.intersection(columns)})
        if table == 'taste_axes' and 'lean' not in columns:
            groups.clear()
        if recommendation_selection and table == 'recommendation_generation_surfaces':
            groups['selection'] = ['selected']
        keys = _KEY_COLUMNS.get(table, ())
        if table == 'choice_learning_publications' and 'document' in columns:
            keys = 'surface'
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
                if group == 'selection' and table == 'recommendation_generation_surfaces' and event != 'UPDATE':
                    continue
                condition = 'record_key=' + _literal('group:' + group)
                if event == 'UPDATE':
                    condition = f'({condition} AND ({changed(selected)}))'
                elif group == 'selection' and table == 'group_generations':
                    condition = f"({condition} AND {sides[0]}.is_active=1)"
                matches.append(condition)
            when = f' WHEN {changed(columns)}' if event == 'UPDATE' else ''
            name = f'group_preview_changed_{table}_{event.lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {event} ON main.{_identifier(table)}{when} BEGIN '
                + invalidate + ' AND EXISTS('
                'SELECT 1 FROM group_preview_dependencies WHERE table_name=' + _literal(table) +
                ' AND (' + ' OR '.join(matches) + ')); END')


def install(conn, context=None):
    for _, sql in definitions(conn):
        conn.execute(sql)


def install_guards(conn, *, sources=_INITIAL_SOURCES):
    for name, sql in guards(conn, sources=sources):
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def validate(conn):
    _validate_definitions(conn, definitions(conn))


def install_catalog_scope_guards(conn, context):
    install_guards(conn, sources=_CATALOG_SCOPE_SOURCES)
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate_catalog_scope_guards(conn):
    _validate_definitions(conn, guards(conn, sources=_CATALOG_SCOPE_SOURCES))


def install_library_readiness_guards(conn, context):
    install_guards(conn, sources=_LIBRARY_READINESS_SOURCES)
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate_library_readiness_guards(conn):
    _validate_definitions(conn, guards(conn, sources=_LIBRARY_READINESS_SOURCES))


def _validate_definitions(conn, expected):
    from .schema_inspection import definitions as read_definitions
    expected = dict(expected)
    actual = read_definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible adaptive preview checkpoint: ' + name)
