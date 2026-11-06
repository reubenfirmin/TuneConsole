"""Exact regular-admission oracle for the installed v103/v107 fixtures."""
from yt_playlist.core import (
    album_feedback_outbox,
    collection_recovery_epoch, credit_retention, embedding_files, group_checkpoint_retention, group_progress, group_retention_progress,
    legacy_journals, metadata_native, metadata_runtime, mix_choices,
    mode_retention, policy_history, profile_files, recommendation_revalidations, served_facts, vibe_provenance,
    vibe_versions, tv_launch_state,
)
from yt_playlist.core.migrations import current, migrate, regular
from yt_playlist.core.preparation import installed_genre_contract
from yt_playlist.repos import vibe_models


CHANGE_NAMES = (
    'independent_playback_feedback_receipts', 'group_preparation_failure_progress',
    'retired_genre_choice_capture', 'retired_taste_axis_capture',
    'retired_metadata_capture', 'resumable_group_retirement',
    'bounded_installed_genre_repair', 'installed_taste_guard_lifecycle',
    'runtime_metadata_file_storage', 'compact_served_facts',
    'displayed_mix_choice_descriptors', 'compact_artist_delivery_replays',
    'retired_taste_mode_payloads', 'native_metadata_delivery_freshness',
    'file_owned_prediction_provenance', 'file_owned_vibe_guards',
    'monotonic_vibe_calculation_ids', 'file_owned_musical_profiles',
    'file_owned_embeddings', 'file_owned_legacy_journals',
    'protected_file_provenance_inputs', 'compact_historical_policy_identities',
    'vibe_version_identities', 'continuing_historical_policy_attribution',
    'continuing_installed_embedding_order',
    'grouping_checkpoint_retention',
    'collection_recovery_ownership_epoch',
    'collection_recovery_scan_keys',
    'revalidated_recommendation_pools',
    'durable_album_feedback_responses',
    'shared_vibe_model_generations',
    'bounded_embedding_basis_guards',
    'durable_initial_tv_launches',
    'masked_vibe_metadata_guards',
)


def captured(conn):
    return {name: sorted((tuple(row) for row in conn.execute('SELECT * FROM "'+name+'"')), key=repr)
            for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def admit_regular_schema(store, *, clock=1234):
    """Preserve every original owner, allowing only named DDL and seed rows.

    Read the domain declarations, not the runner's apply callbacks. Unknown
    registry entries, tables, changed programs, data or histories fail here.
    These fixtures have no unfinished preparation capture to retire.
    """
    conn = store.conn
    before = captured(conn)
    assert 'schema_changes' not in before
    assert tuple(change.name for change in regular.CHANGES) == CHANGE_NAMES
    assert tuple(change.version for change in regular.CHANGES) == tuple(range(1, 35))
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] in (103, 107)
    definitions = dict(conn.execute('SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL'))
    allowed = {'schema_changes': regular.HISTORY_SQL,
               'metadata_native_delivery_heads': metadata_native.RUNTIME_HEAD_SQL,
               'prediction_provenance': vibe_provenance.SQL,
               'album_feedback_response_outbox': album_feedback_outbox.SQL,
               'ix_album_feedback_response_pending': album_feedback_outbox.INDEX}
    for declarations in (
        group_progress.definitions(conn), group_retention_progress.definitions(conn),
        installed_genre_contract.definitions(conn), metadata_runtime.definitions(),
        served_facts.definitions(), mix_choices.definitions(), vibe_provenance.guards(),
        profile_files.definitions(), embedding_files.definitions(),
        vibe_provenance.lifecycle_sql(), policy_history.definitions(),
        vibe_versions.definitions(), policy_history.capture_guards(),
        embedding_files.legacy_order_definitions(),
        group_checkpoint_retention.definitions(conn),
        collection_recovery_epoch.definitions(conn),
        collection_recovery_epoch.key_definitions(conn),
        recommendation_revalidations.definitions(),
        vibe_models.schema(conn),
        embedding_files.bounded_basis_guards(),
        tv_launch_state.definitions(conn),
        vibe_models.metadata_guards(conn),
    ):
        allowed.update(declarations)
    for sql in (*credit_retention.statements(), *mode_retention.statements(),
                *vibe_provenance.allocation_sql(), legacy_journals.SQL, legacy_journals.CUTOVER_SQL):
        allowed[sql.split()[2]] = sql

    # New owners, including changes 29-31, start empty. Existing owners retain
    # their complete captured contents; joint-vibe guards do not seed settings.
    additions = {name: [] for name, sql in allowed.items()
                 if sql.startswith('CREATE TABLE') and name not in before}
    additions.update({
        'schema_changes': sorted(((version, name, float(clock))
                                  for version, name in enumerate(CHANGE_NAMES, 1)), key=repr),
        'group_preparation_progress': [tuple(row) for row in conn.execute(
            'SELECT singleton,phase,reported_at,reason FROM group_preparation_state')],
        # Change 26 adds checkpoint custody guards and invalidates the old
        # retirement closure once, without scanning or rewriting its members.
        'group_retention_epoch': [(1, 1)],
        'collection_recovery_epoch': [(1, 0)],
        'metadata_runtime_state': [(1, 0, 0, 0, 0)],
        'served_fact_state': [(1, 0, 1)],
        'artist_credit_retention': [(1, 0, 0, 0, 0)],
        'taste_mode_retention': [(1, 0, 0, max((row[0] for row in before['taste_modes']), default=0), 0)],
        'prediction_id_state': [(1, max((row[0] for row in before['predictions']), default=0))],
    })
    compatibility = migrate(conn, clock=lambda: clock)
    assert compatibility == (len(before['schema_migrations']) < current.VERSION)
    expected = {**before, **additions}
    actual_rows = captured(conn)
    changed = {name: (expected.get(name), actual_rows.get(name))
               for name in expected.keys() | actual_rows.keys()
               if expected.get(name) != actual_rows.get(name)}
    assert not changed, changed
    actual = dict(conn.execute('SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL'))
    assert {name: current._tokens(sql) for name, sql in actual.items()} == {
        name: current._tokens(sql) for name, sql in {**definitions, **allowed}.items()}
    assert regular.validate(conn) == len(CHANGE_NAMES)
    return captured(conn)
