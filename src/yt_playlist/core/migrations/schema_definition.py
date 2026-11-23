"""Compiled release-baseline schema and current structural validation."""
from collections import Counter, defaultdict
from contextlib import closing
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

from .. import prepared_input_schema
from ..file_receipts import has_native_annotation_constraint
from ..schema_contract import MigrationError, table_clauses, tokens as _tokens


CONTRACT_FLOOR = 107
VERSION = 107
CURRENT_DEFINITION = Path(__file__).resolve().parents[1] / 'schema' / f'layout-{VERSION}.sql'
CURRENT_SEEDS = CURRENT_DEFINITION.with_name(f'seeds-{VERSION}.sql')
DEFINITIONS = (CURRENT_DEFINITION, CURRENT_SEEDS, CURRENT_DEFINITION.with_name('current.json'))
SEED_SETTINGS = {
    'catalog_identity_version': '2',
    'discovery_enrichment_audit_requeued': '1',
    'enrich_ts_backfilled': '1',
    'enrich_unattempted_batch_repaired': '1',
    'like_ratchet_repaired': 'adjusted 0 of 0 like-touched axis weight(s); 0 like row(s) stamped '
                            'sync-provenance; 0 once-ever graduation stamp(s) backfilled',
    'playlist_created_at_backfilled': '1',
}
INITIAL_STATE = (
    "INSERT INTO rec_change_queue(kind,item,revision) VALUES ('refresh','',1)",
    'INSERT INTO rec_preparation VALUES (1,0,0)',
    'INSERT INTO rec_group_clock VALUES (1,0)',
    'INSERT INTO group_retention_state VALUES (1,0)',
    "INSERT INTO rec_vibe_pending(position,group_key,revision,cursor) VALUES (1,'*',2,'')",
)
def statements(*, version=None):
    if version not in (None, VERSION):
        raise MigrationError('Historical schema definitions have been retired')
    yield from _read_statements(CURRENT_DEFINITION)


def seed_statements():
    yield from _read_statements(CURRENT_SEEDS)


def _read_statements(path):
    pending = ''
    for line in path.read_text(encoding='utf-8').splitlines(keepends=True):
        if not pending and (not line.strip() or line.lstrip().startswith('--')):
            continue
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete current schema definition: ' + path.name)


def _checks(sql):
    return _check_tokens(_tokens(sql))


def _check_tokens(tokens):
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


def _foreign_key_deferrals(tokens):
    """SQLite's FK pragma omits deferral; retain it on its actual owning key."""
    result = Counter()
    for clause in table_clauses(tokens):
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
    from .. import schema_inspection
    return schema_inspection.table(conn, name, sql, lambda: _inspect_table(conn, name, sql))


@lru_cache(maxsize=1024)
def _table_declarations(sql):
    """Immutable facts from exact DDL, independent of live SQLite observations."""
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
    return (tuple(_check_tokens(tokens).items()), flags, tuple(annotations.items()),
            tuple(_foreign_key_deferrals(tokens).items()))


def _inspect_table(conn, name, sql):
    quoted = '"' + name.replace('"', '""') + '"'
    # Structure observations are bounded by the declared schema. Fetch each
    # result once rather than crossing the instrumented DB handle per column,
    # key and index during every migration's admission check.
    columns = {r[1]: (r[2].upper(), r[3], r[4], r[5], r[6])
               for r in conn.execute(f'PRAGMA main.table_xinfo({quoted})').fetchall()}
    grouped_keys = defaultdict(list)
    for row in conn.execute(f'PRAGMA main.foreign_key_list({quoted})').fetchall():
        grouped_keys[row[0]].append(row)
    foreign_keys = Counter(tuple(tuple(r[2:]) for r in sorted(rows, key=lambda r: r[1]))
                           for rows in grouped_keys.values())
    unique = Counter()
    for index in conn.execute(f'PRAGMA main.index_list({quoted})').fetchall():
        if index[3] in ('u', 'pk'):
            index_name = '"' + index[1].replace('"', '""') + '"'
            unique[(index[3], tuple((r[2], r[3], r[4]) for r in conn.execute(
                f'PRAGMA main.index_xinfo({index_name})').fetchall() if r[5]))] += 1
    checks, flags, annotations, deferrals = _table_declarations(sql)
    # All counts come from observed declarations and are strictly positive.
    # Plain mappings preserve multiplicity and compare/copy directly; Counter's
    # missing/zero normalization adds work to every unchanged-table admission.
    return (columns, dict(foreign_keys), dict(unique), dict(checks), flags,
            dict(annotations), dict(deferrals))


def _schema_contract(statements):
    return _copy_contract(_reference_contract(tuple(statements)))


def _copy_contract(observations):
    from ..schema_inspection import _copy_table

    # Only reference DDL is cached. Every caller receives its own mappings;
    # adjusting a predecessor's accepted owners cannot change another contract.
    return {name: (kind, _copy_table(value) if kind == 'table' else value)
            for name, (kind, value) in observations.items()}


_REFERENCE_CONNECTION = ContextVar('schema_reference_connection', default=None)


@lru_cache(maxsize=16)
def _reference_contract(statements):
    """Private reference observations, keyed by the complete ordered SQL."""
    return _build_reference_contract(statements)


def _build_reference_contract(statements):
    if not statements:
        return {}
    reference = sqlite3.connect(':memory:')
    token = _REFERENCE_CONNECTION.set(reference)
    try:
        # This private reference is discarded after its observations are read.
        # Build all of its DDL in one transaction instead of committing SQLite's
        # schema bookkeeping after every individual table, index and trigger.
        reference.execute('BEGIN')
        for sql in statements:
            reference.execute(sql)
        return {
            name: (kind, _reference_table(name, sql) if kind == 'table' else _tokens(sql))
            for name, kind, sql in reference.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        }
    finally:
        _REFERENCE_CONNECTION.reset(token)
        reference.close()


@lru_cache(maxsize=1024)
def _reference_table(name, sql):
    """An unchanged reference table has the same declared constraints in every layout.

    Only shipped CREATE TABLE text is reused here. Observe the private contract
    database already built by the caller, rather than constructing another
    database for every table. The cache retains values, never connections.
    Live observations still belong to their connection's admission session.
    """
    if (reference := _REFERENCE_CONNECTION.get()) is not None:
        return _inspect_table(reference, name, sql)
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.execute(sql)
        return _inspect_table(reference, name, sql)


@lru_cache(maxsize=1)
def contract():
    return _schema_contract(statements())


@lru_cache(maxsize=8)
def _vibe_score_contract(normalized, has_definitions, retained_measurements=False, inline_provenance=False):
    from .. import destination_schema, vibe_definitions, vibe_versions
    from . import measurement_storage

    measurements = next(sql for sql in destination_schema.statements(normalized=False)
                        if re.match(r'CREATE TABLE "?measurements"?(?=\s|\()', sql, re.I))
    if normalized:
        measurements = vibe_versions.transform(measurements, 'measurements')
    tables = r'TABLE "?(?:predictions|recording_vibes)"?'
    relations = r'(?:VIEW|TRIGGER) vibe_(?:predictions|recording_fits)(?:_insert)?'
    definitions = [measurements, *(sql for sql in destination_schema.statements(normalized=normalized)
                   if re.match(rf'\s*CREATE (?:{tables}|{relations})(?=\s|\()', sql, re.I))]
    if normalized and not inline_provenance:
        prior = destination_schema.definition().get('prediction_predecessor_statements', ())
        if prediction := next((sql for sql in prior if _tokens(sql)[:3] ==
                               ('create', 'table', 'predictions')), None):
            definitions = [prediction if _tokens(sql)[:3] == ('create', 'table', 'predictions') else sql
                           for sql in definitions]
    if not has_definitions:
        definitions = [sql.replace(', ' + vibe_definitions.REFERENCE, '') for sql in definitions]
    if retained_measurements:
        definitions.extend(('ALTER TABLE measurements ADD COLUMN ' + measurement_storage.ROOT_COLUMN,
                            measurement_storage.UPDATE_GUARD, measurement_storage.DELETE_GUARD))
    return _schema_contract(definitions)


@lru_cache(maxsize=2)
def _annotation_file_contract(native):
    from .. import destination_schema
    from ..file_receipts import native_annotation_definition

    sql = next(sql for sql in destination_schema.statements(normalized=False)
               if re.match(r'\s*CREATE TABLE "?audio_annotations"?(?=\s|\()', sql, re.I))
    return _schema_contract((native_annotation_definition(sql) if native else sql,))


@lru_cache(maxsize=1)
def _audio_archive_predecessor():
    from .. import acousticbrainz_vibe_schema, destination_schema
    from . import annotation_storage

    owners = {annotation_storage.TABLE, *annotation_storage.RETIRED, acousticbrainz_vibe_schema.INPUTS}
    names = owners | set(annotation_storage.REMOVED_GUARDS) | {
        'rec_musical_links', acousticbrainz_vibe_schema.ESTIMATES, acousticbrainz_vibe_schema.SERVED}
    earlier = _schema_contract(destination_schema.statements(normalized=False))
    return {name: definition for name, definition in earlier.items()
            if name in names or (definition[0] in ('index', 'trigger') and 'on' in definition[1]
                                 and definition[1][definition[1].index('on') + 1] in owners)}


@lru_cache(maxsize=3)
def _retired_metadata_contract(unified=False, has_originals=True):
    from .. import metadata_locator_contract as locators
    from ..track_sources import folded_foreign_keys
    table = folded_foreign_keys(locators.TABLE) if unified else locators.TABLE
    if has_originals:
        objects = _schema_contract([table, 'CREATE TABLE metadata_legacy_records(id INTEGER)', *locators.GUARDS.values()])
        return {name: value for name, value in objects.items() if name != 'metadata_legacy_records'}
    from .retired_catalog import ORIGINAL_COLUMNS
    # Match the compact delivery layout with the nullable provenance columns.
    columns = ''.join(f', {name} {declaration}' for name, declaration in ORIGINAL_COLUMNS.items())
    table = table.replace(',\n CHECK(provider_entity_id', columns + ',\n CHECK(provider_entity_id', 1)
    return _schema_contract([table, locators.GUARDS['immutable_metadata_delivery'], locators.GUARDS[locators.MARKER]])


@lru_cache(maxsize=1)
def _metadata_predecessor_contract():
    from .retired_catalog import predecessor_definitions
    prior = predecessor_definitions()
    expected = _schema_contract(sql for sql in prior if sql.startswith('CREATE TABLE '))
    for sql in prior:
        _, kind, name, _ = sql.split(maxsplit=3)
        if kind != 'TABLE':
            expected[name] = (kind.lower(), _tokens(sql))
    return expected


def _album_predecessor_contract():
    from .. import destination_schema

    return _predecessor_contract(destination_schema.definition().get('album_predecessor_statements', ()))


def _predecessor_contract(prior):
    return _copy_contract(_reference_predecessor(tuple(prior)))


@lru_cache(maxsize=128)
def _reference_predecessor(prior):
    # Many historical fragments are checked together. Cache them separately so
    # they cannot evict the complete layouts used at every migration step.
    tables = [sql for sql in prior if sql.startswith('CREATE TABLE ')]
    expected = _build_reference_contract(tables)
    for sql in prior:
        match = re.match(r'CREATE (?:UNIQUE )?(INDEX|VIEW|TRIGGER) "?([a-z_0-9]+)"?', sql)
        if match:
            kind, name = match.groups()
            expected[name] = kind.lower(), _tokens(sql)
    return expected


def bootstrap(conn, registry, now, *, validation=None):
    """Initialize the complete current state in the runner's single transaction."""
    if len(registry) < VERSION:
        raise MigrationError('Current bootstrap is ahead of the migration registry')
    for sql in statements():
        conn.execute(sql)
    from yt_playlist.core import genre_identity
    genre_identity.seed_catalog(conn)
    for sql in seed_statements():
        conn.execute(sql)
    conn.executemany('INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)',
                     [(m.version, m.name, now) for m in registry[:VERSION]])
    from yt_playlist.core import group_progress
    group_progress.install(conn)
    (validation or validate)(conn)


def _legacy_track_view():
    sql = next(sql for sql in statements() if sql.startswith('CREATE VIEW catalog_tracks ')).rstrip(';')
    choice = "FROM entry_field_choices ch WHERE ch.entry_id=e.id AND ch.field='energy'"
    return sql.replace(f"CASE WHEN EXISTS(SELECT 1 {choice}) THEN (SELECT json_extract(ch.value_json,'$') {choice}) "
                       "ELSE r.energy END AS energy,", '')


def validate(conn, *, foreign_keys=True, data=True):
    from .. import acousticbrainz_vibe_schema, catalog_metadata, destination_schema, evidence_maintenance, recording_library, recording_metadata
    from .. import content_work, publication_files, recommendation_custody as custody, recording_selection, taste_axes, track_sources, vibe_versions
    from . import (account_binding_receipt, account_bindings, account_storage, action_collection_storage,
                             album_feedback_effects, album_playback_storage, album_storage,
                             annotation_storage, artist_credit_replay_storage, artist_details, audio_playback_storage, choice_evidence_storage, choice_learned_settings, choice_learning_storage,
                             content_work_settings, control_settings_storage, discovery_artists,
                             discovery_checkpoints, discovery_impression_storage, discovery_tracks, embedding_storage, experiment_decision_storage,
                             genre_storage,
                             group_context_owners, group_generation_storage, group_input_retention_storage, group_preview_evidence_storage,
                             group_publication_storage,
                             held_recommendation_state, history_worker_storage, home_storage, home_taste_settings, import_and_fit_receipts,
                             listener_output_storage, maintenance_clocks,
                             metadata_capture_storage, metadata_choice_storage, metadata_conflict_storage, metadata_delivery_storage, metadata_hint_storage, metadata_history_owners, metadata_observation_storage, metadata_selection_owners,
                             legacy_mode_storage, mix_menu_storage, mix_worker_storage,
                             model_artifacts_storage, model_selection_storage, musical_observation_storage, musical_source_profile_storage, musical_source_queue_storage,
                             paid_access_owner, paid_access_settings, paid_description_storage, paid_usage, playback_binding_storage, playback_response_receipts,
                             play_event_storage, playlist_bookkeeping, playlist_document_storage,
                             playlist_items_storage, policy_archive_storage, policy_body_settings, policy_history_settings, policy_worker_storage,
                             playlist_merge_preferences, playlist_scope_cleanup, prepared_input_storage,
                             mood_taste_storage, preparation_checkpoint_storage, prepared_display_settings,
                             provider_field_storage, publication_manifests, shared_file_receipts,
                             radio_discovery_details, radio_launch_storage, radio_policy_storage, radio_session_storage, rating_action_events,
                             group_evidence_references, group_evidence_settings, group_input_digests, group_versions_by_generation,
                             recommendation_build_state, recommendation_candidate_storage, recommendation_chunk_receipts, recommendation_custody, recommendation_membership, recommendation_receipt_storage,
                             radio_tv_settings, recommendation_refresh_storage, recommendation_storage, report_part_storage, road_trip_storage,
                             recording_association_storage, recording_content_work, recording_profile_storage, recording_redirect_storage, retired_catalog,
                             runtime_choice_owners, served_impression_storage, spotify_records_storage, startup_worker_storage, sync_cursor_storage, sync_settings, taste_axis_storage, track_association_history, tv_draw_storage, tv_playlist_recovery, tv_preparation_details, vibe_preparation_storage,
                             vibe_model_receipts, vibe_worker_storage,
                             work_sequences, worker_checkpoints)

    applied = destination_schema.applied_changes(conn)
    complete = len(applied) == len(destination_schema.definition()['regular_history'])
    unified = track_sources.folded(conn)
    if unified:
        track_sources.validate(conn)
        expected = _schema_contract(destination_schema.statements())
        from ..schema_inspection import catalog_rows
        installed = {name for name, _, _ in catalog_rows(conn)}
        if not complete:
            # Later regular changes can still be pending on a unified install.
            # Validate the baseline and every installed object before applying
            # them. At the complete history the full compiled contract applies.
            required = contract().keys() | {'track_sources', 'write_unresolved_items'}
            expected = {name: definition for name, definition in expected.items() if name in required | installed}
            if legacy_mode_storage.RETIRED in installed:
                prior = destination_schema.definition().get(legacy_mode_storage.PREDECESSOR_KEY, ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if rating_action_events.RETIRED in installed:
                prior = destination_schema.definition().get('rating_action_event_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if policy_history_settings.RETIRED in installed:
                prior = destination_schema.definition().get('policy_history_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if group_evidence_settings.RETIRED in installed:
                prior = destination_schema.definition().get('group_evidence_settings_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if policy_body_settings.RETIRED in installed:
                prior = destination_schema.definition().get('policy_body_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if playback_response_receipts.POINT not in installed:
                prior = destination_schema.definition().get('playback_response_receipt_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_chunk_receipts.RETIRED in installed:
                prior = destination_schema.definition().get('recommendation_chunk_receipt_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if recommendation_chunk_receipts.BY_ID in installed:
                    predecessor.pop(recommendation_chunk_receipts.RECEIPTS)
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if metadata_history_owners.RETIRED in installed:
                prior = destination_schema.definition().get('metadata_history_owner_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if metadata_history_owners.preparing(conn):
                    for owner in metadata_history_owners.OWNERS:
                        predecessor.pop(owner)
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if runtime_choice_owners.RETIRED in installed:
                prior = destination_schema.definition().get('runtime_choice_owner_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if metadata_selection_owners.RETIRED in installed:
                prior = destination_schema.definition().get('metadata_selection_owner_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if metadata_selection_owners.preparing(conn):
                    for owner in metadata_selection_owners.OWNERS.values():
                        predecessor.pop(owner)
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if group_context_owners.RETIRED in installed:
                prior = destination_schema.definition().get('group_context_owner_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if custody.HOME_RETIREMENT_GUARD not in installed:
                prior = destination_schema.definition().get('home_recommendation_root_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if publication_files.INDEX not in installed:
                prior = destination_schema.definition().get('prepared_display_file_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if content_work.RECORDING_INDEX not in installed:
                prior = destination_schema.definition().get('recording_content_work_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if discovery_impression_storage.RETIRED in installed:
                prior = destination_schema.definition().get('discovery_impression_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if experiment_decision_storage.RETIRED in installed:
                prior = destination_schema.definition().get('experiment_decision_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if playback_binding_storage.RETIRED in installed:
                prior = destination_schema.definition().get('playback_binding_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if track_association_history.RETIRED in installed:
                prior = destination_schema.definition().get('track_association_history_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if metadata_choice_storage.RETIRED in installed:
                prior = destination_schema.definition().get('metadata_choice_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if artist_credit_replay_storage.RETIRED in installed:
                prior = destination_schema.definition().get('artist_credit_replay_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if metadata_hint_storage.RETIRED in installed:
                prior = destination_schema.definition().get('metadata_hint_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_custody.RETIRED in installed:
                prior = destination_schema.definition().get('recommendation_custody_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_build_state.RETIRED in installed:
                prior = destination_schema.definition().get('recommendation_build_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if content_work_settings.RETIRED in installed:
                prior = destination_schema.definition().get('content_work_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if prepared_display_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('prepared_display_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if choice_learned_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('choice_learned_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if spotify_records_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('spotify_records_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if sync_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('sync_settings_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if tv_playlist_recovery.RETIRED[0] in installed:
                prior = destination_schema.definition().get('tv_playlist_recovery_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if radio_tv_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('radio_tv_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if home_taste_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('home_taste_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if 'youtube_playback_alternates' in installed:
                prior = destination_schema.definition().get('playback_preference_predecessor_statements', ())
                expected.update(_predecessor_contract(prior))
            if action_collection_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('action_collection_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if model_artifacts_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('model_artifact_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if musical_observation_storage.RETIRED in installed:
                prior = destination_schema.definition().get('musical_observation_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if musical_observation_storage.PREPARATION in installed:
                    source = musical_observation_storage.SOURCE
                    predecessor.pop(source, None)
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if tv_preparation_details.RETIRED[0] in installed:
                prior = destination_schema.definition().get('tv_preparation_detail_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if radio_discovery_details.RETIRED[0] in installed:
                prior = destination_schema.definition().get('radio_discovery_detail_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if mood_taste_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('mood_taste_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if provider_field_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('provider_field_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if provider_field_storage.PREPARATION in installed:
                    for owner in provider_field_storage.OWNERS:
                        predecessor.pop(owner)
                    expected.update(_predecessor_contract(provider_field_storage.preparation_indexes()))
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if shared_file_receipts.RETIRED[0] in installed:
                prior = destination_schema.definition().get('shared_file_receipt_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            elif shared_file_receipts.STATE_CHANGE not in applied:
                expected[shared_file_receipts.TABLE][1][0].pop(evidence_maintenance.COLUMN, None)
            if choice_evidence_storage.RETIRED[-1] in installed:
                prior = destination_schema.definition().get('choice_evidence_predecessor_statements', ())
                predecessor = _predecessor_contract(prior)
                if choice_evidence_storage.PENDING_INDEX in installed:
                    predecessor.pop(choice_evidence_storage.IMPRESSIONS)
                    expected.update(_predecessor_contract((choice_evidence_storage.PENDING_INDEX_SQL,)))
                expected.update((name, definition) for name, definition in predecessor.items() if name in installed)
            if publication_manifests.RETIRED[0] in installed:
                prior = destination_schema.definition().get('publication_manifest_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if playlist_document_storage.RETIRED[1] in installed:
                prior = destination_schema.definition().get('playlist_document_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if preparation_checkpoint_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('preparation_checkpoint_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if listener_output_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('listener_output_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if control_settings_storage.RETIRED[1] in installed:
                prior = destination_schema.definition().get('control_settings_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if group_versions_by_generation.RETIRED[0] in installed:
                prior = destination_schema.definition().get('group_versions_by_generation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            for owner, key in ((group_input_digests, 'group_input_digest'),
                               (group_evidence_references, 'group_evidence_reference'), (report_part_storage, 'report_part'),
                               (recommendation_receipt_storage, 'recommendation_receipt')):
                if owner.RETIRED in installed:
                    prior = destination_schema.definition().get(key + '_predecessor_statements', ())
                    expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                    if name in installed)
            if prepared_input_storage.RETIRED in installed:
                prior = destination_schema.definition().get('prepared_input_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_membership.RETIRED in installed:
                prior = destination_schema.definition().get('recommendation_membership_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if metadata_conflict_storage.RETIRED[0] in installed:
                prior = destination_schema.definition().get('metadata_conflict_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if playlist_bookkeeping.RETIRED[0] in installed:
                prior = destination_schema.definition().get('playlist_bookkeeping_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if playlist_merge_preferences.RETIRED in installed:
                expected[playlist_merge_preferences.RETIRED] = contract()[playlist_merge_preferences.RETIRED]
            if audio_playback_storage.RETIRED in installed:
                expected[audio_playback_storage.RETIRED] = contract()[audio_playback_storage.RETIRED]
                expected.update(_schema_contract((annotation_storage.SQL[annotation_storage.TABLE],)))
            if album_playback_storage.RETIRED in installed:
                prior = destination_schema.definition().get('album_playback_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if paid_description_storage.RETIRED in installed:
                prior = destination_schema.definition().get('paid_description_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if vibe_model_receipts.RETIRED in installed:
                prior = destination_schema.definition().get('vibe_model_receipt_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if 'sync_attempts' in installed:
                prior = destination_schema.definition().get('sync_recovery_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if 'tv_pool_state' in installed:
                prior = destination_schema.definition().get('tv_storage_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if 'spotify_import_job_head' in installed:
                prior = destination_schema.definition().get('spotify_storage_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if radio_policy_storage.MARKER not in installed:
                prior = destination_schema.definition().get('radio_policy_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and not name.startswith('collection_recovery_changed_'))
            if metadata_capture_storage.RETIRED in installed:
                prior = destination_schema.definition().get('metadata_capture_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            for change, owner in ((import_and_fit_receipts, 'import_and_fit'),
                                  (startup_worker_storage, 'startup_worker'), (policy_worker_storage, 'policy_worker'),
                                  (tv_draw_storage, 'tv_draw'), (history_worker_storage, 'history_worker')):
                if change.NAME not in applied:
                    prior = destination_schema.definition().get(owner + '_predecessor_statements', ())
                    expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                    if name in installed)
            if vibe_worker_storage.NAME not in applied:
                prior = destination_schema.definition().get('vibe_worker_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if maintenance_clocks.NAME not in applied:
                prior = destination_schema.definition().get('maintenance_clock_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if 'group_preview_work' in installed:
                prior = destination_schema.definition().get('group_preview_evidence_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if held_recommendation_state.NAME not in applied:
                prior = destination_schema.definition().get('held_recommendation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if mix_worker_storage.NAME not in applied:
                prior = destination_schema.definition().get('mix_worker_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if work_sequences.NAME not in applied:
                prior = destination_schema.definition().get('work_sequences_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if sync_cursor_storage.RETIRED in installed:
                expected[sync_cursor_storage.TABLE] = contract()[sync_cursor_storage.TABLE]
            if vibe_preparation_storage.TABLE in installed:
                expected[vibe_preparation_storage.TABLE] = contract()[vibe_preparation_storage.TABLE]
            if musical_source_profile_storage.MARKER not in installed:
                prior = destination_schema.definition().get('musical_source_profile_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            # Generation-owned versions also remove the old manifest index.
            if group_input_retention_storage.MARKER not in installed and group_versions_by_generation.NAME not in applied:
                prior = destination_schema.definition().get('group_input_retention_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if taste_axes.GRADUATIONS not in installed:
                prior = destination_schema.definition().get('taste_axis_graduation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if not taste_axis_storage.installed(conn):
                prior = destination_schema.definition().get('taste_axis_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recording_redirect_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recording_redirect_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recording_profile_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recording_profile_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if musical_source_queue_storage.MARKER not in installed:
                prior = destination_schema.definition().get('musical_source_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if embedding_storage.MARKER not in installed:
                prior = destination_schema.definition().get('embedding_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recording_association_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recording_association_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if policy_archive_storage.MARKER not in installed and policy_history_settings.RETIRED in installed:
                prior = destination_schema.definition().get('policy_archive_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if road_trip_storage.MARKER not in installed:
                prior = destination_schema.definition().get('road_trip_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if served_impression_storage.MARKER not in installed:
                prior = destination_schema.definition().get('served_impression_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            compact_selection = model_selection_storage.NAME in applied
            if not compact_selection:
                prior = destination_schema.definition().get('model_selection_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if worker_checkpoints.NAME not in applied:
                prior = destination_schema.definition().get('worker_checkpoint_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed and (compact_selection or name != worker_checkpoints.MODELS))
            if mix_menu_storage.MARKER not in installed:
                prior = destination_schema.definition().get('mix_menu_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_refresh_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recommendation_refresh_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if paid_access_owner.NAME not in applied:
                prior = destination_schema.definition().get('paid_access_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if metadata_observation_storage.MARKER not in installed:
                prior = destination_schema.definition().get('metadata_observation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            elif metadata_observation_storage.GUARD_REPAIR not in applied:
                expected.update(metadata_observation_storage.previous_selection_guards(conn))
            if recommendation_candidate_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recommendation_candidate_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if group_generation_storage.MARKER not in installed:
                prior = destination_schema.definition().get('group_generation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            # Shared receipts replace the delivery storage index.
            if (metadata_delivery_storage.MARKER not in installed
                    and shared_file_receipts.MARKER not in installed):
                prior = destination_schema.definition().get('metadata_delivery_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if recommendation_storage.MARKER not in installed:
                prior = destination_schema.definition().get('recommendation_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            elif recommendation_storage.PREVIEW_REVISION not in applied:
                expected[recommendation_storage.PREVIEW_UPDATE] = (
                    'trigger', _tokens(recommendation_storage.preview_guard(conn, selection=False)))
            if radio_session_storage.MARKER not in installed:
                earlier = _schema_contract(destination_schema.statements(normalized=False))
                for name, (kind, definition) in earlier.items():
                    if name in {'radio_sessions', *radio_session_storage.RETIRED} or (
                        kind == 'trigger' and 'on' in definition
                        and definition[definition.index('on') + 1] in radio_session_storage.RETIRED
                    ):
                        expected[name] = kind, definition
            elif radio_launch_storage.INDEX not in installed:
                prior = destination_schema.definition().get('radio_launch_predecessor_statements', ())
                expected.update(_predecessor_contract(prior))
            if not album_storage.installed(conn):
                expected.update((name, definition) for name, definition in _album_predecessor_contract().items()
                                if name in required | installed)
            if playlist_items_storage.MARKER not in installed:
                prior = destination_schema.definition().get('playlist_item_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if account_binding_receipt.NAME not in applied:
                prior = destination_schema.definition().get('account_binding_receipt_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if not account_storage.applied(conn):
                expected.update(account_storage.predecessor_contract(conn, contract()))
            elif account_bindings.CURRENT_INDEX not in installed:
                prior = destination_schema.definition()['account_binding_predecessor_statements']
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if playlist_scope_cleanup.DECISIONS not in installed:
                baseline = contract()
                expected['playlist_scopes'] = baseline['playlist_scopes']
                expected.update((name, baseline[name]) for name in playlist_scope_cleanup.RETIRED if name in installed)
            if choice_learned_settings.MARKER not in installed and not choice_learning_storage.installed(conn):
                owners = set(choice_learning_storage.FOREIGN_KEY_OWNERS)
                preview_guards = {f'group_preview_changed_{table}_{event}' for table in owners
                                  for event in ('insert', 'update', 'delete')}
                earlier = _schema_contract(destination_schema.statements(normalized=False))
                expected.update((name, (kind, definition)) for name, (kind, definition) in earlier.items()
                                if name in owners | preview_guards
                                or (kind in ('index', 'trigger') and 'on' in definition
                                    and definition[definition.index('on') + 1] in owners))
            if album_feedback_effects.MARKER not in installed:
                sql = next(sql for sql in destination_schema.statements()
                           if _tokens(sql)[:3] == ('create', 'table', 'album_feedback'))
                for column, declaration in album_feedback_effects.COLUMNS:
                    sql = sql.replace(f', {column} {declaration}', '', 1)
                expected.update(_predecessor_contract((sql, album_feedback_effects.SQL, album_feedback_effects.INDEX)))
            if paid_access_settings.NAME not in applied:
                expected.update(_schema_contract((paid_usage.ACCESS,)))
            if 'rec_group_clock' in installed:
                prior = destination_schema.definition().get('group_checkpoint_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if home_storage.MARKER not in installed and prepared_display_settings.RETIRED[0] in installed:
                prior = destination_schema.definition().get('home_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if discovery_artists.ARTIST_INDEX not in installed:
                prior = destination_schema.definition().get('discovery_artist_predecessor_statements', ())
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if discovery_checkpoints.MARKER not in installed:
                prior = destination_schema.definition()['discovery_checkpoint_predecessor_statements']
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if discovery_tracks.TRACK_INDEX not in installed:
                prior = destination_schema.definition()['discovery_track_predecessor_statements']
                expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                if name in installed)
            if 'genre_redirects' in installed:
                expected.update(genre_storage.predecessor_contract(conn, contract()))
                expected['catalog_tracks'] = ('view', _tokens(catalog_metadata.track_view(conn, canonical=True)))
            if 'metadata_legacy_records' in installed:
                expected.update(_metadata_predecessor_contract())
                if retired_catalog.ORIGINAL_INDEX in installed:
                    expected[retired_catalog.ORIGINAL_INDEX] = ('index', _tokens(
                        'CREATE UNIQUE INDEX ' + retired_catalog.STAGED_INDEX_TARGET))
            if 'group_build_versions' in installed:
                earlier = _schema_contract(destination_schema.statements(normalized=False))
                expected.update((name, earlier[name]) for name in group_publication_storage.PREDECESSOR_OBJECTS)
            if play_event_storage.PROJECTION_NAME not in applied:
                earlier = _schema_contract(destination_schema.statements(normalized=False))
                expected.update((name, earlier[name]) for name in play_event_storage.EVIDENCE_VIEWS)
            if 'listening_capture_receipts' in installed:
                earlier = _predecessor_contract(destination_schema.definition()['play_event_predecessor_statements'])
                if not account_storage.applied(conn):
                    earlier = {name: definition for name, definition in earlier.items()
                               if name not in {'play_events', *account_storage.RETIRED_GUARDS}}
                expected.update((name, definition) for name, definition in earlier.items() if name in installed)
            if 'audio_taxonomies' in installed:
                expected.update(_audio_archive_predecessor())
            elif (acousticbrainz_vibe_schema.CLASSIFIER_INDEX in installed
                  and acousticbrainz_vibe_schema.INDEXED_VIEW_CHANGE not in applied):
                expected[acousticbrainz_vibe_schema.SERVED] = (
                    'view', _tokens(acousticbrainz_vibe_schema.LINKED_CLASSIFIER_VIEW))
            if recording_library.LISTENING_PROBES_NAME not in applied:
                earlier = _predecessor_contract(recording_library.listening_probe_predecessors(conn))
                expected.update((name, definition) for name, definition in earlier.items() if name in installed)
        if musical_observation_storage.PREPARATION in installed:
            # Compilation can prepare against the previous shipped definition.
            source = musical_observation_storage.SOURCE
            columns, _, _, checks, *_ = expected[source][1]
            if 'selected_observations' not in columns:
                _, (extra_columns, _, _, extra_checks, *_) = _predecessor_contract(
                    (f'CREATE TABLE {source} ({musical_observation_storage.COLUMN})',))[source]
                columns.update(extra_columns)
                checks.update(extra_checks)
            expected.update(_predecessor_contract(musical_observation_storage.preparation_indexes()))
        if not complete and artist_details.MARKER not in installed:
            expected.pop(artist_details.MARKER, None)
            expected.pop('retained_artist_credit_identity', None)
            prior = destination_schema.definition().get('artist_predecessor_statements', ())
            expected.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                            if name in installed)
        if not complete and group_publication_storage.installed_with_build_reference(conn):
            # An installed C95 briefly used a foreign key and retained the old
            # publication guard. Admit that exact layout so C98 can repair it.
            statements = destination_schema.definition()['group_generation_predecessor_statements']
            prior = next(sql for sql in destination_schema.statements()
                         if sql.startswith('CREATE TABLE group_versions '))
            prior = prior.replace(group_publication_storage.BUILD_COLUMN, group_publication_storage.BUILD_REFERENCE)
            expected.update(_schema_contract((prior,)))
            definition = next(sql for sql in statements
                              if sql.startswith('CREATE TRIGGER complete_group_publication '))
            expected['complete_group_publication'] = ('trigger', _tokens(definition.replace(
                group_publication_storage.STAGED_VERSIONS, group_publication_storage.RETIRED_STAGED_VERSIONS)))
            for name in group_publication_storage.BUILD_GUARDS:
                expected.pop(name, None)
    else:
        expected = (_schema_contract(destination_schema.statements(normalized=False)) if complete
                    else dict(contract()))
        if vibe_versions.normalized(conn) and not catalog_metadata._has_energy(conn):
            expected['catalog_tracks'] = ('view', _tokens(_legacy_track_view()))
            compiled = _schema_contract(destination_schema.statements())
            compiled.update(_predecessor_contract(destination_schema.definition().get(
                'prepared_input_predecessor_statements', ())))
            for name in ('recordings', 'prepared_recording_inputs', 'unresolved_preparation_inputs',
                         'rec_capture_recordings_update', 'rec_content_recordings_update',
                         'group_preview_changed_recordings_update',
                         *(name for name, _sql in prepared_input_schema.definitions())):
                if name in expected:
                    expected[name] = compiled[name]
            kind, (columns, *constraints) = expected['recordings']
            if recording_metadata.installed(conn):
                columns = {**columns, **{name: definition for name, definition in
                          _schema_contract(destination_schema.statements(normalized=False))['recordings'][1][0].items()
                          if name in recording_metadata.COLUMNS}}
            else:
                columns = {name: column for name, column in columns.items() if name not in recording_metadata.COLUMNS}
            expected['recordings'] = (kind, (columns, *constraints))
    pending_columns = set()
    if not recording_library.installed(conn):
        pending_columns.update(recording_library.COLUMNS)
    if not recording_library.discovery_installed(conn):
        pending_columns.add(recording_library.DISCOVERY_COLUMN)
    if unified and not recording_selection.installed(conn):
        pending_columns.update(recording_selection.COLUMNS)
    if pending_columns:
        kind, (columns, *constraints) = expected['recordings']
        columns = {name: column for name, column in columns.items() if name not in pending_columns}
        expected['recordings'] = (kind, (columns, *constraints))
    _validate(conn, expected, VERSION, foreign_keys=foreign_keys, data=data)


def _validate(conn, expected_contract, version, *, replaced=frozenset(), foreign_keys=True, data=True):
    from .. import metadata_runtime, metadata_selections, runtime_metadata_choices
    from .. import catalog_metadata, recording_metadata
    from .. import destination_schema, legacy_journals
    from .. import track_sources, vibe_definitions, vibe_versions
    from .. import metadata_locator_contract as metadata_locators
    from .. import library_scope_changes
    from . import (action_cleanup, annotation_storage, artist_storage, control_settings_storage,
                             measurement_storage, prediction_storage)
    from ..schema_inspection import catalog_rows
    # Preserve the full-contract check's last matching object, including a
    # trigger that reuses a table name. Named legacy lookups use the first row.
    objects = {name: (kind, sql) for name, kind, sql in catalog_rows(conn)}
    runtime_metadata_active = False
    if 'metadata_runtime_choices' in objects or runtime_metadata_choices.VIEW in objects:
        runtime_metadata_active = (control_settings_storage.predecessor_metadata_active(conn)
                                   if 'metadata_runtime_state' in objects else metadata_runtime.active(conn))
    compact_actions = 'uq_actions_draft_signature' in objects
    action_shape = 'uq_actions_draft_signature' if compact_actions else 'action_drafts'
    if action_shape not in expected_contract:
        expected_contract = {**expected_contract, **action_cleanup.contract(
            compact=compact_actions, memberships_folded=track_sources.folded(conn) and
            'playlist_membership_refs' not in objects)}
    if compact_actions:
        replaced = replaced | action_cleanup.REMOVED
    compact_artists = artist_storage.MARKER in objects
    artist_shape = artist_storage.MARKER if compact_artists else 'artist_scans'
    if artist_shape not in expected_contract:
        unified = track_sources.folded(conn)
        artist_contract = artist_storage.contract(compact=compact_artists, unified=unified)
        if unified and not compact_artists and 'discovery_legacy_offers' not in objects:
            from ..discovery_provenance import EARLIER_OFFERS
            from ..domain_contracts import EARLIER_DISCOVERY_OFFERS

            prior = [sql for sql in statements() if sql.startswith('CREATE TABLE artists ')]
            prior.extend(f'ALTER TABLE artists ADD COLUMN {column} {declaration} '
                         f'CHECK({EARLIER_DISCOVERY_OFFERS[column]})'
                         for column, _, declaration in EARLIER_OFFERS)
            artist_contract = {**artist_contract, **_schema_contract(prior)}
        expected_contract = {**expected_contract, **artist_contract}
    if compact_artists:
        replaced = replaced | artist_storage.TABLES
    if metadata_locators.retired(conn):
        from .. import group_preview_work
        metadata_locators.validate(conn, active=runtime_metadata_active)
        preview_guards = {name: ('trigger', _tokens(sql)) for name, sql
                          in group_preview_work.guards(conn, sources={'metadata_deliveries'})
                          if name in expected_contract}
        retired = _retired_metadata_contract(track_sources.folded(conn), 'metadata_legacy_records' in objects)
        expected_contract = {**expected_contract, **retired, **preview_guards}
        if 'metadata_delivery_locations' in expected_contract:
            expected_contract.update(_schema_contract(metadata_locators._application_relations(conn).values()))
        replaced = replaced | metadata_locators.REMOVED
    if 'album_feedback_legacy_dismissals' in objects and 'album_feedback' in expected_contract:
        expected_contract = {**expected_contract, 'album_feedback': contract()['album_feedback']}
    from . import paid_usage
    if paid_usage.OLD in objects:
        expected_contract = {**expected_contract, **{name: contract()[name] for name in (
            *paid_usage.OWNERS, 'provider_usage_update', paid_usage.RETIRED_INDEX)
            if name in expected_contract}}
    if 'legacy_archive_cutovers' in objects and 'legacy_archive_heads' in expected_contract:
        expected_contract = {**expected_contract, **_schema_contract((legacy_journals.SQL, legacy_journals.CUTOVER_SQL))}
    if legacy_journals.FILE_REFERENCES in expected_contract:
        expected_contract = {**expected_contract, legacy_journals.FILE_REFERENCES: (
            'view', _tokens(legacy_journals.file_references_sql(objects)))}
    if 'recording_embedding_legacy_order' in objects:
        from .. import embedding_files
        prior_embeddings = {name: ('trigger', _tokens(sql)) for name, sql in embedding_files.legacy_order_guards()}
        prior_embeddings.update({name: contract()[name] for name in ('artist_embeddings', 'recording_embeddings')})
        expected_contract = {**expected_contract, **{name: definition for name, definition in prior_embeddings.items()
                                                     if name in expected_contract}}
    if 'discovery_legacy_offers' in objects and track_sources.folded(conn):
        from ..discovery_provenance import OFFER_OWNERS
        from ..domain_contracts import EARLIER_DISCOVERY_OFFERS

        original = contract()
        for name in ('album_discovery_memberships', 'discovered_artists', 'discovered_albums', 'discovered_albums_update'):
            expected_contract[name] = original[name]
        for name in OFFER_OWNERS.values():
            kind, (columns, foreign_keys_expected, unique, checks, flags, annotations, deferrals) = expected_contract[name]
            for column, check in EARLIER_DISCOVERY_OFFERS.items():
                columns.pop(column, None)
                checks.pop(_tokens(check), None)
            expected_contract[name] = (kind, (columns, foreign_keys_expected, unique, checks, flags, annotations, deferrals))
    if 'schema_changes' in objects and library_scope_changes.applied(conn):
        library_scope_changes.validate(conn)
        replaced = replaced | set(library_scope_changes.RETIRED_VIEWS)
        if not track_sources.folded(conn):
            # The scope change replaces this view during the installed upgrade.
            # Its definition was checked above even if later changes are pending.
            replaced = replaced | {'track_sources'}
    normalized_vibes = vibe_versions.normalized(conn)
    has_vibe_definitions = vibe_definitions.TABLE in objects
    retained_measurements = 'measurements_model_input_delete' in objects
    prediction_provenance_folded = prediction_storage.MARKER in objects
    if normalized_vibes or has_vibe_definitions:
        score_contract = _vibe_score_contract(normalized_vibes, has_vibe_definitions, retained_measurements,
                                             prediction_provenance_folded)
        expected_contract = {**expected_contract, **{name: definition for name, definition in score_contract.items()
            if name in expected_contract and not (name == 'predictions' and prediction_provenance_folded)}}
    if not prediction_provenance_folded and track_sources.folded(conn):
        prior = destination_schema.definition().get('prediction_predecessor_statements', ())
        expected_contract = {name: definition for name, definition in expected_contract.items()
                             if name not in prediction_storage.INDEXES}
        expected_contract.update((name, definition) for name, definition in _predecessor_contract(prior).items()
                                 if name in objects)
    if retained_measurements:
        replaced = replaced | measurement_storage.RETIRED
    elif 'measurements_immutable' in expected_contract:
        expected_contract = {**expected_contract, 'measurements_immutable': contract()['measurements_immutable']}
    if annotation_storage.TABLE in expected_contract and 'audio_taxonomies' in objects:
        # Both the release input and the migrated ID-file constraint are known.
        expected_contract = {**expected_contract, **_annotation_file_contract(has_native_annotation_constraint(conn))}
    if 'legacy_archive_heads' in objects:
        replaced = replaced | legacy_journals.retired_relations(conn)
    from . import metadata_observation_storage
    runtime_views = (metadata_runtime.views(compact=metadata_observation_storage.MARKER in objects,
                         selection_relation=metadata_selections.VIEW if metadata_selections.VIEW in objects
                         else 'recording_metadata_selections')
                     if runtime_metadata_active else {})
    if track_sources.folded(conn):
        runtime_views = {name: track_sources.folded_sql(sql) for name, sql in runtime_views.items()}
    if 'catalog_metadata_state' in objects:
        from .. import listening_summaries
        expected_contract = {**expected_contract, 'listening_summary_catalog_entries_update': (
            'trigger', _tokens(listening_summaries.CATALOG_LINK_UPDATE))}
    if 'recording_metadata_state' in objects and not track_sources.folded(conn):
        from .. import listening_summaries
        expected_contract = {**expected_contract, 'listening_summary_recordings_update': (
            'trigger', _tokens(listening_summaries.RECORDING_IDENTITY_UPDATE))}
    if 'catalog_metadata_state' in objects and catalog_metadata.ready(conn):
        replaced = replaced | {'entry_field_choices'}
        runtime_views['catalog_tracks'] = catalog_metadata.track_view(
            conn, canonical=recording_metadata.uses_canonical_view(conn))
    elif 'catalog_metadata_state' in objects:
        # The compiled empty destination has already cut over. A populated
        # installation continues to use the release view during preparation.
        original = contract()
        expected_contract = {**expected_contract, **{name: original[name] for name in ('catalog_tracks', 'entry_field_choices')}}
        if not catalog_metadata._has_energy(conn):
            expected_contract['catalog_tracks'] = ('view', _tokens(_legacy_track_view()))
    file_vibe_guards = {}
    from .. import recommendation_revalidations
    if recommendation_revalidations.available(conn):
        recommendation_revalidations.validate(conn)
        file_vibe_guards.update(recommendation_revalidations.guards(
            group_scoped='group_version_id' in objects['recommendation_pools'][1],
            retirement_inline='retiring INTEGER' in objects['recommendation_pools'][1]))
    if not prediction_provenance_folded and 'schema_changes' in objects and conn.execute(
            "SELECT 1 FROM schema_changes WHERE version=16 AND name='file_owned_vibe_guards'").fetchone():
        from .. import vibe_provenance
        file_vibe_guards.update(vibe_provenance.guards())
    if 'historical_impression_policies' in objects and 'schema_changes' in objects and conn.execute(
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
            if not expected[0].items() <= observed[0].items() or observed[1:] != expected[1:]:
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
    keys = tuple(SEED_SETTINGS)
    slots = ','.join('?' for _ in keys)
    initialized = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (' + slots + ')', keys).fetchall())
    for key in keys:
        if key not in initialized:
            raise MigrationError(f'Missing initialized schema setting: {key}')
    if initialized['catalog_identity_version'] != '2':
        raise MigrationError('Incompatible initialized catalog identity version')
    if data:
        _validate_data(conn, version, compact_artists=compact_artists)
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
        account = 'account_id' if version >= 103 else 'identity_id'
        if 'listening_capture_receipts' in objects:
            capture_check = ('SELECT 1 FROM listening_observations o JOIN listening_capture_receipts c '
                            f'ON c.id=o.capture_id WHERE o.{account}<>c.{account} '
                            'OR o.first_observed_at IS NOT c.captured_at OR o.last_observed_at IS NOT c.captured_at '
                            "OR o.source_day IS NOT date(c.captured_at,'unixepoch') "
                            "OR o.day IS NOT date(c.captured_at,'unixepoch') LIMIT 1")
        else:
            capture_check = ("SELECT 1 FROM listening_observations WHERE capture_id IS NOT NULL AND "
                             "(last_observed_at IS NOT first_observed_at OR source_day IS NOT date(first_observed_at,'unixepoch') "
                             "OR day IS NOT source_day) LIMIT 1")
        if data and conn.execute(capture_check).fetchone():
            raise MigrationError('Historical capture receipt does not match its observation')


def _validate_data(conn, version=VERSION, *, compact_artists=False):
    from .. import discovery_candidates, track_sources
    from . import account_bindings, account_storage

    unified = track_sources.folded(conn)
    source = 'track_sources' if unified else 'catalog_entries'
    representative_check = (
        'SELECT 1 FROM recordings r JOIN track_sources e ON e.id=r.discovery_entry_id '
        'WHERE r.id<>e.recording_id LIMIT 1' if discovery_candidates.tracks_folded(conn) else
        f'SELECT 1 FROM discovery_memberships d JOIN {source} e ON e.id=d.entry_id '
        'WHERE d.recording_id<>e.recording_id LIMIT 1')
    account = 'account_id' if version >= 103 else 'identity_id'
    scope = 'playlist_scope_id' if version >= 103 else 'scope_ref'
    scope_key = 'id' if version >= 103 else 'ref'
    provider = 'provider_playlist_id' if version >= 103 else 'provider_id'
    playlist_provider = 'provider_playlist_id' if version >= 103 else 'ytm_playlist_id'
    canonical_scope = unified and conn.execute('SELECT 1 FROM schema_changes WHERE name=?',
                                              (account_bindings.NAME,)).fetchone() is not None
    provider_match = '' if canonical_scope else f'AND s.{provider}=p.{playlist_provider} '
    folded_memberships = unified and account_storage.applied(conn)
    if not folded_memberships and conn.execute(
            'SELECT 1 FROM playlists p LEFT JOIN playlist_membership_refs m ON m.id=p.id '
            f'WHERE m.id IS NULL OR m.{scope} IS NOT p.{scope} OR m.{account} IS NOT p.{account} LIMIT 1').fetchone():
        raise MigrationError('Invalid retained playlist membership')
    scope_filter = 'p.account_id IS NOT NULL AND ' if folded_memberships else ''
    retained_scope = "OR (p.is_current=0 AND (s.kind<>'account' OR s.account_id=p.account_id)) " if folded_memberships else ''
    checks = (
        ('Invalid generated-play policy',
         'SELECT 1 FROM play_events WHERE is_generated IS NOT NULL AND is_generated NOT IN (0,1) LIMIT 1'),
        ('Missing exact-event calendar projection',
         'SELECT 1 FROM play_events pe LEFT JOIN play_event_calendar c ON c.event_id=pe.id WHERE c.event_id IS NULL LIMIT 1'),
        ('Missing exact-event source contribution',
         'SELECT 1 FROM play_events pe WHERE NOT EXISTS (SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id) LIMIT 1'),
        ('Invalid playlist scope ownership',
         f'SELECT 1 FROM playlists p WHERE {scope_filter}'
         f'NOT EXISTS (SELECT 1 FROM playlist_scopes s WHERE s.{scope_key}=p.{scope} '
         f"{provider_match}AND ((s.kind='account' AND s.{account}=p.{account} "
         f"AND s.{provider} IN ('LM','SE')) OR (s.kind='shared' AND s.{account} IS NULL "
         f"AND s.{provider} NOT IN ('LM','SE')) {retained_scope})) LIMIT 1"),
        ('Colliding playlist positions',
         'SELECT 1 FROM playlist_tracks GROUP BY playlist_id,position HAVING COUNT(*)>1 LIMIT 1'),
        ('Invalid playlist position',
         "SELECT 1 FROM playlist_tracks WHERE typeof(position)<>'integer' OR position<0 LIMIT 1"),
        ('Discovery representative belongs to another recording', representative_check),
    )
    for message, sql in checks:
        if conn.execute(sql).fetchone():
            raise MigrationError(message)
    redirects = ('SELECT id AS retired_id,redirect_artist_id AS artist_id FROM artists WHERE redirect_artist_id IS NOT NULL'
                 if compact_artists else 'SELECT retired_id,artist_id FROM artist_redirects')
    if conn.execute(
            f'WITH redirects AS ({redirects}) SELECT 1 FROM redirects r '
            'JOIN artists old ON old.id=r.retired_id JOIN artists target ON target.id=r.artist_id '
            'WHERE old.provisional<>1 OR target.provisional<>0 '
            'OR EXISTS(SELECT 1 FROM redirects WHERE retired_id=r.artist_id) '
            'OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.retired_id) '
            'OR NOT EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=r.artist_id) LIMIT 1').fetchone():
        raise MigrationError('Artist redirect requires a provisional source and canonical target')
    for table in (('discovered_artists',) if compact_artists else ('artist_scans', 'discovered_artists', 'rec_artist_similar')):
        if conn.execute(f'SELECT 1 FROM {table} WHERE artist_id IS NULL LIMIT 1').fetchone():
            raise MigrationError(f'Unresolved artist state: {table}')
