"""Versioned current bootstrap and schema contract, independent of the baseline.

Recognized unversioned databases and every numbered predecessor remain supported
through the frozen historical upgrade path. Empty databases start directly at
VERSION. A later schema replacement must publish its own current definition.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

from .runner import MigrationError


CONTRACT_FLOOR = 32
VERSION = 107
DEFINITION = Path(__file__).with_name('schema.sql')
DEFINITIONS = (DEFINITION.with_name('schema_discovery_subjects.sql'),
               DEFINITION.with_name('schema_listening_subjects.sql'),
               DEFINITION, DEFINITION.with_name('schema_metadata_deliveries.sql'),
               DEFINITION.with_name('schema_archives.sql'), DEFINITION.with_name('schema_scope_account_state.sql'),
               DEFINITION.with_name('schema_metadata_evidence.sql'), DEFINITION.with_name('schema_playback_feedback.sql'),
               DEFINITION.with_name('schema_recommendation_work.sql'), DEFINITION.with_name('schema_home_offers.sql'),
               DEFINITION.with_name('schema_album_note_requests.sql'),
               DEFINITION.with_name('schema_genre_preferences.sql'),
               DEFINITION.with_name('schema_genre_classifications.sql'),
               DEFINITION.with_name('schema_recommendation_requests.sql'),
               DEFINITION.with_name('schema_taste_axes.sql'),
               DEFINITION.with_name('schema_recipe_memberships.sql'),
               DEFINITION.with_name('schema_listening_outputs.sql'),
               DEFINITION.with_name('schema_home_outputs.sql'),
               DEFINITION.with_name('schema_choice_learning.sql'),
               DEFINITION.with_name('schema_recommendation_reports.sql'),
               DEFINITION.with_name('schema_collection_state.sql'),
               DEFINITION.with_name('schema_session_state.sql'),
               DEFINITION.with_name('schema_sync_state.sql'),
               DEFINITION.with_name('schema_model_state.sql'),
               DEFINITION.with_name('schema_tv_recommendations.sql'),
               DEFINITION.with_name('schema_clicked_library_pools.sql'),
               DEFINITION.with_name('schema_model_projections.sql'),
               DEFINITION.with_name('schema_radio_policy_roots.sql'),
               DEFINITION.with_name('schema_taste_controls.sql'),
               DEFINITION.with_name('schema_collection_documents.sql'),
               DEFINITION.with_name('schema_action_work.sql'),
               DEFINITION.with_name('schema_transfer_work.sql'),
               DEFINITION.with_name('schema_album_occurrences.sql'),
               DEFINITION.with_name('schema_foreign_calendar_scopes.sql'))
SEED_SETTINGS = {
    'catalog_identity_version': '2',
    'discovery_enrichment_audit_requeued': '1',
    'enrich_ts_backfilled': '1',
    'enrich_unattempted_batch_repaired': '1',
    'like_ratchet_repaired': 'adjusted 0 of 0 like-touched axis weight(s); 0 like row(s) stamped '
                            'sync-provenance; 0 once-ever graduation stamp(s) backfilled',
    'playlist_created_at_backfilled': '1',
}
TOKEN = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                   r"`(?:``|[^`])*`|\[[^]]*\]|[A-Za-z_]\w*|\S", re.S)
_migration_version = ContextVar('schema_definition_version',default=None)


@contextmanager
def migration_definitions(version):
    """Historical callers without a version see their own frozen schema."""
    token = _migration_version.set(version)
    try:
        yield
    finally:
        _migration_version.reset(token)


def statements(*, version=None):
    if version is None:
        version = _migration_version.get() or VERSION
    from . import v103_entity_names, v104_domain_contracts
    for sql in _statements(version):
        if version >= 103:
            sql = v103_entity_names.transform(sql)
        yield v104_domain_contracts.transform(sql) if version >= 104 else sql
    if version >= 105:
        from . import v105_frozen_share_membership
        yield from v105_frozen_share_membership.fences()


def _statements(version):
    from . import v086_genre_classifications as genres
    from . import v088_taste_axes as axes
    from . import v089_recipe_memberships as recipes
    from . import v094_collection_state as collections
    from . import v097_collection_documents as documents
    from . import v098_action_work as actions
    from . import v099_album_occurrences as album_occurrences
    from . import v100_listening_subjects as listening_subjects
    from . import v101_discovery_subjects as discovery_subjects
    from . import v102_catalog_writes as catalog_writes
    for definition in DEFINITIONS:
        if version < 106 and definition.name == 'schema_foreign_calendar_scopes.sql':
            continue
        if version < 101 and definition.name == 'schema_discovery_subjects.sql':
            continue
        if version < 100 and definition.name == 'schema_listening_subjects.sql':
            continue
        if version < 99 and definition.name == 'schema_album_occurrences.sql':
            continue
        if version < 98 and definition.name in ('schema_action_work.sql','schema_transfer_work.sql'):
            continue
        if version < 97 and definition.name == 'schema_collection_documents.sql':
            continue
        if version < 96 and definition.name == 'schema_taste_controls.sql':
            continue
        if version < 95 and definition.name in ('schema_tv_recommendations.sql','schema_clicked_library_pools.sql',
                                               'schema_model_projections.sql','schema_radio_policy_roots.sql'):
            continue
        if version < 94 and definition.name in collections.DEFINITIONS:
            continue
        if version < 93 and definition.name in ('schema_choice_learning.sql','schema_recommendation_reports.sql'):
            continue
        if version < 91 and definition.name == 'schema_home_outputs.sql':
            continue
        if version < 90 and definition.name == 'schema_listening_outputs.sql':
            continue
        if version < 89 and definition.name == 'schema_recipe_memberships.sql':
            continue
        if version < 88 and definition.name == 'schema_taste_axes.sql':
            continue
        if version < 87 and definition.name == 'schema_recommendation_requests.sql':
            continue
        if version < 86 and definition.name == 'schema_genre_classifications.sql':
            continue
        pending = ''
        for line in definition.read_text().splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                if definition.name=='schema_radio_policy_roots.sql' and re.match(
                        r'CREATE (?:TABLE |INDEX ix_)radio_seed_policies\b',pending.lstrip(),re.I):
                    pending = ''
                    continue
                sql = genres.transform(pending) if version >= 86 else pending
                sql = axes.transform(sql) if version >= 88 else sql
                sql = recipes.transform(sql) if version >= 89 else sql
                if definition == DEFINITION:
                    sql = collections.transform(sql) if version >= 94 else sql
                    sql = documents.transform(sql) if version >= 97 else sql
                    sql = actions.transform(sql) if version >= 98 else sql
                    sql = album_occurrences.transform(sql) if version >= 99 else sql
                    sql = listening_subjects.transform(sql) if version >= 100 else sql
                if version >= 101 and definition.name!='schema_discovery_subjects.sql':
                    sql = discovery_subjects.transform(sql)
                if version >= 102:
                    sql = catalog_writes.transform(sql)
                yield sql
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete current schema definition: ' + definition.name)
    if version >= 93:
        from . import v093_choice_learning
        yield from v093_choice_learning.fences()
    if version >= 94:
        yield from collections.settings_fences()
    if version >= 95:
        from . import v095_recommendation_state, v095_saved_outputs
        from yt_playlist.core import model_projections
        yield from v095_recommendation_state.model_control_statements()
        yield from model_projections.guards()
        yield from v095_recommendation_state.retired_fences()
        yield from v095_saved_outputs.definitions()
    if version >= 96:
        from . import v096_taste_controls
        yield from v096_taste_controls.fences()
    if version >= 97:
        yield from documents.fences()
    if version >= 98:
        yield from actions.fences()
    if version >= 99:
        yield from album_occurrences.fences()
    if version >= 100:
        yield from listening_subjects.fences()
    if 101 <= version < 102:
        yield from discovery_subjects.fences()


@lru_cache(maxsize=4096)
def _tokens(sql):
    result = []
    for match in TOKEN.finditer(sql or ''):
        token = match.group()
        if token.startswith(('--', '/*')):
            continue
        if token.startswith("'"):
            result.append(token)
        elif token.startswith(('"', '`', '[')):
            result.append(token[1:-1].replace(token[0] * 2, token[0]).lower())
        else:
            result.append(token.lower())
    return tuple(result)


def _checks(sql):
    tokens = _tokens(sql)
    checks = []
    for start, token in enumerate(tokens[:-1]):
        if token != 'check' or tokens[start + 1] != '(':
            continue
        depth = 0
        for end in range(start + 1, len(tokens)):
            depth += (tokens[end] == '(') - (tokens[end] == ')')
            if depth == 0:
                checks.append(tokens[start + 2:end])
                break
    return Counter(checks)


def _table_clauses(sql):
    """Split column/table constraints without splitting composite keys or expressions."""
    tokens = _tokens(sql)
    start = tokens.index('(') + 1
    depth = 0
    for position in range(start, len(tokens)):
        token = tokens[position]
        if depth == 0 and token in (',', ')'):
            yield tokens[start:position]
            start = position + 1
            if token == ')':
                return
        depth += (token == '(') - (token == ')')


def _foreign_key_deferrals(sql):
    """SQLite's FK pragma omits deferral; retain it on its actual owning key."""
    result = Counter()
    for clause in _table_clauses(sql):
        depth = 0
        top_level = []
        for position, token in enumerate(clause):
            if depth == 0:
                top_level.append(position)
            depth += (token == '(') - (token == ')')
        reference = next((p for p in top_level if clause[p] == 'references'), None)
        if reference is None:
            continue
        declaration = clause[2:] if clause[0] == 'constraint' else clause
        if declaration[:2] == ('foreign', 'key'):
            children = tuple(t for t in declaration[3:declaration.index(')')] if t != ',')
        else:
            children = (clause[0],)
        parent = clause[reference + 1]
        parent_columns = ()
        if clause[reference + 2:reference + 3] == ('(',):
            end = clause.index(')', reference + 3)
            parent_columns = tuple(t for t in clause[reference + 3:end] if t != ',')
        deferral = ()
        for position in top_level:
            if position > reference and clause[position] == 'deferrable':
                start = position - (clause[position - 1] == 'not')
                end = position + (3 if clause[position + 1:position + 2] == ('initially',) else 1)
                deferral = clause[start:end]
        result[(children, parent, parent_columns, deferral)] += 1
    return result


def _table(conn, name, sql):
    quoted = '"' + name.replace('"', '""') + '"'
    columns = {r[1]: (r[2].upper(), r[3], r[4], r[5], r[6]) for r in conn.execute(f'PRAGMA main.table_xinfo({quoted})')}
    grouped_keys = defaultdict(list)
    for row in conn.execute(f'PRAGMA main.foreign_key_list({quoted})'):
        grouped_keys[row[0]].append(row)
    foreign_keys = Counter(tuple(tuple(r[2:]) for r in sorted(rows, key=lambda r: r[1]))
                           for rows in grouped_keys.values())
    unique = Counter()
    for index in conn.execute(f'PRAGMA main.index_list({quoted})'):
        if index[3] in ('u', 'pk'):
            index_name = '"' + index[1].replace('"', '""') + '"'
            unique[(index[3], tuple((r[2], r[3], r[4]) for r in conn.execute(
                f'PRAGMA main.index_xinfo({index_name})') if r[5]))] += 1
    tokens = _tokens(sql)
    # table_list resolves columns of every main/TEMP view, even when its name
    # argument or WHERE clause names one table. Repeating that work per owner
    # makes admission expand unrelated nested publication views hundreds of
    # times. SQLite has already validated this stored CREATE TABLE statement;
    # its trailing options contain exactly these two flags. Column names,
    # defaults and CHECK expressions occur before the final closing parenthesis.
    end = max((index for index, token in enumerate(tokens) if token == ')'), default=-1)
    options = tokens[end+1:] if end>=0 else ()
    flags = (int(any(options[index:index+2] == ('without', 'rowid') for index in range(len(options)-1))),
             int('strict' in options))
    annotations = Counter()
    for position, token in enumerate(tokens):
        if token == 'collate':
            annotations[tokens[position:position + 2]] += 1
        elif token == 'autoincrement':
            annotations[(token,)] += 1
    return columns, foreign_keys, unique, _checks(sql), flags, annotations, _foreign_key_deferrals(sql)


def _schema_contract(statements):
    reference = sqlite3.connect(':memory:')
    try:
        for sql in statements:
            reference.execute(sql)
        return {
            name: (kind, _table(reference, name, sql) if kind == 'table' else _tokens(sql))
            for name, kind, sql in reference.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
        }
    finally:
        reference.close()


@lru_cache(maxsize=1)
def contract():
    return _schema_contract(statements())


def bootstrap(conn, registry, now):
    """Initialize the complete current state in the runner's single transaction."""
    if len(registry) < VERSION:
        raise MigrationError('Current bootstrap is ahead of the migration registry')
    for sql in statements():
        conn.execute(sql)
    from yt_playlist.core import genre_identity
    genre_identity.seed_catalog(conn)
    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)', SEED_SETTINGS.items())
    conn.execute("INSERT INTO rec_change_queue(kind,item,revision) VALUES ('refresh','',1)")
    conn.execute('INSERT INTO rec_preparation VALUES (1,0,0)')
    conn.execute('INSERT INTO rec_group_clock VALUES (1,0)')
    conn.execute('INSERT INTO group_retention_state VALUES (1,0)')
    conn.execute("INSERT INTO rec_vibe_pending(position,group_key,revision,cursor) VALUES (1,'*',2,'')")
    conn.executemany('INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)',
                     [(m.version, m.name, now) for m in registry[:VERSION]])
    from yt_playlist.core import group_progress
    group_progress.install(conn)
    validate(conn)


def validate(conn, *, foreign_keys=True, data=True):
    _validate(conn, contract(), VERSION, foreign_keys=foreign_keys, data=data)


def _validate(conn, expected_contract, version, *, replaced=frozenset(), foreign_keys=True, data=True):
    from .. import metadata_runtime
    from .. import legacy_journals
    from .. import vibe_versions
    if vibe_versions.normalized(conn):
        expected_contract = {**expected_contract, **vibe_versions.contract()}
    replaced = replaced | legacy_journals.retired_relations(conn)
    runtime_views = metadata_runtime.views() if metadata_runtime.active(conn) else {}
    objects = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    file_vibe_guards = {}
    if 'schema_changes' in objects and conn.execute(
            "SELECT 1 FROM schema_changes WHERE version=16 AND name='file_owned_vibe_guards'").fetchone():
        from .. import vibe_provenance
        file_vibe_guards = dict(vibe_provenance.guards())
    if 'schema_changes' in objects and conn.execute(
            "SELECT 1 FROM schema_changes WHERE name='continuing_historical_policy_attribution'").fetchone():
        from .. import policy_history
        file_vibe_guards.update(policy_history.capture_guards())
    for name, (kind, expected) in expected_contract.items():
        if name in replaced:
            continue
        if kind in ('index', 'trigger') and 'on' in expected:
            owner = expected[expected.index('on') + 1]
            # DROP TABLE retires its indexes and triggers with the owner. A
            # replaced but still present table keeps its ordinary dependencies.
            if owner in replaced and owner not in objects:
                continue
        actual = objects.get(name)
        if actual is None or actual[0] != kind:
            raise MigrationError(f'Missing current {kind}: {name}')
        if kind == 'table':
            observed = _table(conn, name, actual[1])
            columns, *constraints = expected
            if any(observed[0].get(key) != value for key, value in columns.items()) or observed[1:] != tuple(constraints):
                raise MigrationError(f'Incompatible current table/constraint: {name}')
        else:
            if name in file_vibe_guards:
                expected = _tokens(file_vibe_guards[name])
            if kind == 'view' and name in runtime_views:
                expected = _tokens(runtime_views[name])
            if name == 'retained_group_publication_delete':
                from yt_playlist.core import group_retention_progress
                if group_retention_progress.available(conn):
                    expected = _tokens(group_retention_progress.publication_guard())
            if _tokens(actual[1]) != expected:
                raise MigrationError(f'Incompatible current {kind}: {name}')
    if foreign_keys and conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid current foreign-key reference')
    for key in SEED_SETTINGS:
        if conn.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone() is None:
            raise MigrationError(f'Missing initialized schema setting: {key}')
    identity_version = conn.execute("SELECT value FROM settings WHERE key='catalog_identity_version'").fetchone()
    if identity_version[0] != '2':
        raise MigrationError('Incompatible initialized catalog identity version')
    if data:
        _validate_data(conn,version)
    if version >= 80:
        for table, column in (('rec_musical_sources', 'payload'), ('provider_attempts', 'fields_json'),
                              ('enrichment_provider_state', 'fields_json')):
            if any(row[1] == column for row in conn.execute(f'PRAGMA main.table_xinfo({table})')):
                raise MigrationError(f'Superseded metadata value store remains: {table}.{column}')
        if data and conn.execute('SELECT 1 FROM album_provider_ids GROUP BY album_id,provider '
                        'HAVING SUM(is_primary)<>1 LIMIT 1').fetchone():
            raise MigrationError('Album representations require exactly one preferred provider ID')
        if any(name in objects for name in ('audio_vibe_samples', 'rec_vibe_tracks', 'rec_vibe_groups')):
            raise MigrationError('Superseded vibe stores remain')
        for name in ('rec_mood', 'audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels',
                     'rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors',
                     'rec_artist_vectors', 'rec_artist_content_vectors', 'rec_prepared_content',
                     'rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants',
                     'rec_group_generations', 'rec_group_identities', 'rec_group_definitions', 'rec_group_members',
                     'rec_group_unknown', 'rec_group_lineage', 'rec_group_head', 'rec_group_publications',
                     'rec_group_builds', 'rec_group_graph', 'rec_group_assignments',
                     'rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds',
                     'track_artists', 'metadata_findings', 'identity_state_archive', 'legacy_track_evidence', 'legacy_enrichment_attempts',
                     'playlist_group', 'hidden_playlists', 'overlap_ignored', 'playlist_provenance',
                     'album_library_memberships', 'album_library_revisions', 'album_library_snapshots',
                     'album_library_legacy_cohort'):
            if name in objects:
                raise MigrationError(f'Superseded current owner remains: {name}')
    if version >= 82 and conn.execute(
            "SELECT 1 FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation') LIMIT 1").fetchone():
        raise MigrationError('Retired recommendation work state remains in rec_proposals')
    if version >= 83 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface='home_shown'").fetchone():
        raise MigrationError('Retired Home offer deduplication remains in rec_proposals')
    if version >= 84 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('album_note_requests','album_notes')").fetchone():
        raise MigrationError('Retired album note demand remains in rec_proposals')
    if version >= 87 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN "
            "('album_menu_refresh','home_genre_request','choice_window:albums','choice_window:mixes','choice_window:radio')").fetchone():
        raise MigrationError('Retired recommendation demand remains in rec_proposals')
    if version >= 90 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN "
            "('listening_analytics','listening_edition') OR substr(surface,1,16)='listening_chunk:'").fetchone():
        raise MigrationError('Retired listening display output remains in rec_proposals')
    if version >= 91 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN "
            "('home_prepared','home_tuned','home_choice','album_prepared','adaptive_group_preview',"
            "'home_discovery','home_revisit','into_recently','trend_rollups','cleanup')").fetchone():
        raise MigrationError('Retired Home display output remains in rec_proposals')
    if version >= 93 and conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN "
            "('choice_bandit','group_choice_bandit','choice_interests','listening_choices',"
            "'choice_daily','model_health','model_live_report')").fetchone():
        raise MigrationError('Retired recommendation findings remain in rec_proposals')
    if version >= 95 and conn.execute('SELECT 1 FROM rec_proposals LIMIT 1').fetchone():
        raise MigrationError('Retired generic recommendation state remains')
    if version >= 95 and conn.execute("SELECT 1 FROM settings WHERE key IN ('radio_seed_offer','rec_autotune_result') LIMIT 1").fetchone():
        raise MigrationError('Retired saved recommendation output remains in settings')
    if version >= 96 and conn.execute("SELECT 1 FROM settings WHERE key IN "
            "('taste_settings_checkpoint_v1','taste_comparison_baseline_v2','ppr_experiment_decisions') LIMIT 1").fetchone():
        raise MigrationError('Retired taste control remains in settings')
    if version >= 97 and conn.execute("SELECT 1 FROM settings WHERE key='spotify_library_import_job' LIMIT 1").fetchone():
        raise MigrationError('Retired Spotify import job remains in settings')
    if version >= 33:
        for name in ('history_items', 'history_snapshots', 'legacy_listening_observations'):
            if name in objects:
                raise MigrationError(f'Retired capture structure remains: {name}')
        from . import v103_entity_names
        capture_check = ('SELECT 1 FROM listening_observations o JOIN listening_capture_receipts c '
                        'ON c.id=o.capture_id WHERE o.identity_id<>c.identity_id '
                        'OR o.first_observed_at IS NOT c.captured_at OR o.last_observed_at IS NOT c.captured_at '
                        "OR o.source_day IS NOT date(c.captured_at,'unixepoch') "
                        "OR o.day IS NOT date(c.captured_at,'unixepoch') LIMIT 1")
        if data and conn.execute(v103_entity_names.transform(capture_check) if version>=103 else capture_check).fetchone():
            raise MigrationError('Historical capture receipt does not match its observation')


def _validate_data(conn,version=VERSION):
    checks = (
        ('Invalid generated-play policy',
         'SELECT 1 FROM play_events WHERE is_generated IS NOT NULL AND is_generated NOT IN (0,1) LIMIT 1'),
        ('Missing exact-event calendar projection',
         'SELECT 1 FROM play_events pe LEFT JOIN play_event_calendar c ON c.event_id=pe.id WHERE c.event_id IS NULL LIMIT 1'),
        ('Missing exact-event source contribution',
         'SELECT 1 FROM play_events pe WHERE NOT EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id) LIMIT 1'),
        ('Invalid retained playlist membership',
         'SELECT 1 FROM playlists p LEFT JOIN playlist_membership_refs m ON m.id=p.id '
         'WHERE m.id IS NULL OR m.scope_ref IS NOT p.scope_ref OR m.identity_id IS NOT p.identity_id LIMIT 1'),
        ('Invalid playlist scope ownership',
         'SELECT 1 FROM playlists p WHERE NOT EXISTS (SELECT 1 FROM playlist_scopes s WHERE s.ref=p.scope_ref '
         'AND s.provider_id=p.ytm_playlist_id AND ((s.kind=\'account\' AND s.identity_id=p.identity_id '
         "AND s.provider_id IN ('LM','SE')) OR (s.kind='shared' AND s.identity_id IS NULL "
         "AND s.provider_id NOT IN ('LM','SE')))) LIMIT 1"),
        ('Colliding playlist positions',
         'SELECT 1 FROM playlist_tracks GROUP BY playlist_id,position HAVING COUNT(*)>1 LIMIT 1'),
        ('Invalid playlist position',
         "SELECT 1 FROM playlist_tracks WHERE typeof(position)<>'integer' OR position<0 LIMIT 1"),
        ('Discovery representative belongs to another recording',
         'SELECT 1 FROM discovery_memberships d JOIN catalog_entries e ON e.id=d.entry_id '
         'WHERE d.recording_id<>e.recording_id LIMIT 1'),
        ('Artist redirects must point directly to canonical identities',
         'SELECT 1 FROM artist_redirects r JOIN artist_redirects target ON target.retired_id=r.artist_id LIMIT 1'),
        ('Artist redirect requires a provisional source and canonical target',
         'SELECT 1 FROM artist_redirects r JOIN artists old ON old.id=r.retired_id '
         'JOIN artists target ON target.id=r.artist_id WHERE old.provisional<>1 OR target.provisional<>0 '
         'OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.retired_id) '
         'OR NOT EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.artist_id) LIMIT 1'),
    )
    for message, sql in checks:
        if version>=103:
            from . import v103_entity_names
            sql = v103_entity_names.transform(sql)
        if conn.execute(sql).fetchone():
            raise MigrationError(message)
    for table in ('artist_scans', 'discovered_artists', 'rec_artist_similar'):
        if conn.execute(f'SELECT 1 FROM {table} WHERE artist_id IS NULL LIMIT 1').fetchone():
            raise MigrationError(f'Unresolved artist state: {table}')
