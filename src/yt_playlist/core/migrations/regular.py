"""Transactional schema changes for the installed release.

schema_migrations retains the exact already-applied layout/conversion history.
The regular ledger records each completed change in the same transaction as its SQL.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Callable

from .. import library_taste_summary, listener_state, metadata_locator_contract, vibe_versions
from .. import acousticbrainz_vibe_schema, catalog_metadata, library_scope_changes, recording_library
from .. import recording_metadata, recording_selection, track_sources, vibe_definitions
from .. import embedding_files, file_receipts, group_preview_work, legacy_journals, prepared_input_schema
from . import (account_binding_receipt, account_bindings, account_storage, acousticbrainz_storage, action_cleanup,
               action_collection_storage, album_feedback_effects,
               album_playback_storage, album_storage, annotation_storage, artist_details, audio_playback_storage,
               artist_credit_replay_storage, artist_storage, choice_learned_settings, choice_learning_storage, discovery_artists, discovery_checkpoints, discovery_offer_history, discovery_tracks,
               content_work_settings, control_settings_storage, embedding_storage, listener_output_storage, playlist_document_storage,
               recording_content_work,
               choice_evidence_storage, mood_taste_storage, preparation_checkpoint_storage, provider_field_storage, publication_manifests,
               radio_discovery_details, radio_tv_settings, shared_file_receipts,
               genre_storage, group_checkpoint_storage, group_context_owners, group_evidence_references, group_evidence_settings,
               group_generation_storage,
               group_input_digests, group_input_retention_storage,
               group_preview_evidence_storage, group_publication_storage, group_versions_by_generation, held_recommendation_state, home_storage,
               home_recommendation_roots, home_taste_settings,
               discovery_impression_storage, experiment_decision_storage, group_version_storage, history_worker_storage, import_and_fit_receipts, maintenance_clocks, measurement_storage,
               metadata_capture_storage, metadata_choice_storage, metadata_conflict_storage, metadata_delivery_storage, metadata_hint_storage, metadata_history_owners, metadata_observation_storage, metadata_selection_owners,
               legacy_mode_storage, mix_menu_storage, mix_worker_storage, mode_offer_settings, model_artifacts_storage, model_selection_storage, musical_observation_storage,
               musical_source_profile_storage, musical_source_queue_storage,
               paid_access_owner, paid_access_settings, paid_description_storage, paid_usage,
               play_event_preparation, play_event_storage, playback_binding_storage, playback_preference_storage, playback_response_receipts,
               playlist_bookkeeping, playlist_items_storage, playlist_merge_preferences, playlist_scope_cleanup, policy_archive_storage,
               policy_body_settings, policy_history_settings, policy_worker_storage, prediction_storage, prepared_display_files, prepared_display_settings, prepared_input_storage,
               radio_launch_storage, radio_policy_storage, radio_session_storage, rating_action_events, rebuild_history,
               recommendation_build_state, recommendation_candidate_storage, recommendation_chunk_receipts, recommendation_custody, recommendation_membership, recommendation_receipt_storage,
               recommendation_impression_counts, recommendation_refresh_storage, recommendation_storage, report_part_storage,
               recording_association_storage, recording_profile_storage, recording_redirect_storage,
               retired_catalog, road_trip_storage, runtime_choice_owners, served_impression_storage, spotify_records_storage, spotify_storage, startup_worker_storage, sync_cursor_storage,
               sync_recovery_storage, sync_settings, taste_axis_graduations, taste_axis_storage, track_association_history, track_source_cutover,
               tv_draw_storage, tv_playlist_recovery, tv_preparation_details, tv_storage, vibe_model_receipts, vibe_model_schema, vibe_preparation_storage, vibe_worker_storage,
               wiki_cache_storage, work_sequences, worker_checkpoints)
from .runner import Context, MigrationError, PreparationChanged, history, validate_version


HISTORY_SQL = ('CREATE TABLE schema_changes (version INTEGER PRIMARY KEY,'
               'name TEXT NOT NULL,applied_at REAL NOT NULL)')


@dataclass(frozen=True)
class Change:
    version: int
    name: str
    apply: Callable
    validate: Callable
    replace_tables: bool = False
    prepare: Callable | None = None
    foreign_key_owners: tuple[str, ...] = ()


def _receipt_sql(conn):
    sql = Path(__file__).with_name('schema_playback_feedback.sql').read_text()
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='accounts' AND type='table'").fetchone():
        sql = sql.replace('REFERENCES identities(id)', 'REFERENCES accounts(id)')
    return sql


def validate_receipts(conn):
    from .current import _tokens

    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' "
                       "AND name='playback_feedback_receipts'").fetchone()
    if row is None or _tokens(row[0]) != _tokens(_receipt_sql(conn).strip().rstrip(';')):
        raise MigrationError('Incompatible playback feedback receipt owner')


def _install_receipts(conn, context):
    # Current bootstraps and installed v81+ already own this table. Verify it
    # before recording adoption; IF NOT EXISTS would conceal a wrong layout.
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='playback_feedback_receipts'").fetchone():
        conn.execute(_receipt_sql(conn))


def _install_group_progress(conn, context):
    from .. import group_progress
    group_progress.install(conn)


def _validate_group_progress(conn):
    from .. import group_progress
    group_progress.validate(conn)


def _retire_genre_choice_capture(conn, context):
    _validate_retired_genre_capture(conn)


def _validate_retired_genre_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone():
        names = [f'prepare_genre_choices_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')', names).fetchone():
            raise MigrationError('Retired genre choice capture still references its removed source')


def _retire_taste_capture(conn, context):
    _validate_retired_taste_capture(conn)


def _validate_retired_taste_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone():
        names=[f'prepare_taste_axes_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias','genre_family') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')',names).fetchone():
            raise MigrationError('Retired taste capture still references its removed keys')


def _retire_metadata_capture(conn, context):
    _validate_retired_metadata_capture(conn)


def _validate_retired_metadata_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_observations'").fetchone():
        names = [f'prepare_{job}_dependency_{owner}_{operation}'
            for job in ('metadata','metadata_conflicts')
            for owner in ('catalog_entries','recordings','youtube_items','genre_catalog','genre_alias')
            for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')', names).fetchone():
            raise MigrationError('Retired metadata capture still references its removed sources')


def _install_group_retirement(conn, context):
    from .. import group_retention_progress
    group_retention_progress.install(conn)


def _validate_group_retirement(conn):
    from .. import group_retention_progress
    group_retention_progress.validate(conn)


def _repair_installed_genre(conn, context):
    from .. import genre_schema
    genre_schema.install(conn,context)


def _validate_installed_genre(conn):
    from .. import genre_schema
    # Change 7 could precede the taste owner. Change 8 repairs its known missing
    # guards and establishes strict validation without rewriting applied rows.
    genre_schema.validate(conn, allow_missing_guards=True)


def _validate_installed_genre_guards(conn):
    from .. import genre_schema
    genre_schema.validate(conn)


def _install_runtime_metadata(conn, context):
    from .. import metadata_runtime
    metadata_runtime.install(conn, enrichment_clock=maintenance_clocks.ENRICHMENT_CLOCK)


def _validate_runtime_metadata(conn):
    from .. import metadata_runtime
    metadata_runtime.validate(conn, enrichment_clock=maintenance_clocks.ENRICHMENT_CLOCK)


def _install_served_facts(conn, context):
    from .. import served_facts
    served_facts.install(conn, context)


def _validate_served_facts(conn):
    from .. import served_facts
    served_facts.validate(conn)


def _install_mix_choices(conn, context):
    from .. import mix_choices
    mix_choices.install(conn, context)


def _validate_mix_choices(conn):
    from .. import mix_choices
    mix_choices.validate(conn)


def _install_credit_retention(conn, context):
    from .. import credit_retention
    credit_retention.install(conn, context)


def _validate_credit_retention(conn):
    from .. import credit_retention
    credit_retention.validate(conn)


def _install_mode_retention(conn, context):
    from .. import mode_retention
    mode_retention.install(conn, context)


def _validate_mode_retention(conn):
    from .. import mode_retention
    mode_retention.validate(conn)


def _install_native_metadata_heads(conn, context):
    from .. import metadata_native
    metadata_native.install_runtime_heads(conn, context)


def _validate_native_metadata_heads(conn):
    from .. import metadata_native
    metadata_native.validate_runtime_heads(conn)


def _install_vibe_provenance(conn, context):
    from .. import vibe_provenance
    vibe_provenance.install(conn, context)


def _validate_vibe_provenance(conn):
    from .. import vibe_provenance
    vibe_provenance.validate(conn)


def _install_vibe_file_guards(conn, context):
    from .. import vibe_provenance
    vibe_provenance.install_guards(conn, context)


def _validate_vibe_file_guards(conn):
    from .. import vibe_provenance
    vibe_provenance.validate_guards(conn)


def _install_vibe_allocation(conn, context):
    from .. import vibe_provenance
    vibe_provenance.install_allocation(conn, context)


def _validate_vibe_allocation(conn):
    from .. import vibe_provenance
    vibe_provenance.validate_allocation(conn)


def _install_profile_files(conn, context):
    from .. import profile_files
    profile_files.install(conn, context)


def _validate_profile_files(conn):
    from .. import profile_files
    profile_files.validate(conn)


def _install_embedding_files(conn, context):
    from .. import embedding_files
    embedding_files.install(conn, context)


def _validate_embedding_files(conn):
    from .. import embedding_files
    embedding_files.validate(conn)


def _install_legacy_journals(conn, context):
    legacy_journals.install(conn, context)


def _validate_legacy_journals(conn):
    legacy_journals.validate(conn)


def _install_provenance_lifecycle(conn, context):
    from .. import vibe_provenance
    vibe_provenance.install_lifecycle(conn, context)


def _validate_provenance_lifecycle(conn):
    from .. import vibe_provenance
    vibe_provenance.validate_lifecycle(conn)


def _install_policy_history(conn, context):
    from .. import policy_history
    policy_history.install(conn, context)


def _validate_policy_history(conn):
    from .. import policy_history
    policy_history.validate(conn)


def _install_vibe_versions(conn, context):
    from .. import vibe_versions
    vibe_versions.install(conn, context)


def _validate_vibe_versions(conn):
    from .. import vibe_versions
    vibe_versions.validate(conn)


def after_owner_upgrade(conn):
    """Keep adopted bounded guards when an explicit upgrade creates an owner."""
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='schema_changes'").fetchone():
        return
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=7 AND name='bounded_installed_genre_repair'").fetchone():
        from .. import genre_schema
        genre_schema.install_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=16 AND name='file_owned_vibe_guards'").fetchone():
        from .. import vibe_provenance
        vibe_provenance.install_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='continuing_historical_policy_attribution'").fetchone():
        from .. import policy_history
        policy_history.install_capture(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='continuing_installed_embedding_order'").fetchone():
        from .. import embedding_files
        embedding_files.install_legacy_order_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='grouping_checkpoint_retention'").fetchone():
        from .. import group_checkpoint_retention
        group_checkpoint_retention.install(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='collection_recovery_ownership_epoch'").fetchone():
        from .. import collection_recovery_epoch
        collection_recovery_epoch.install(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='collection_recovery_scan_keys'").fetchone():
        from .. import collection_recovery_epoch
        collection_recovery_epoch.install_keys(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=29 AND name='revalidated_recommendation_pools'").fetchone():
        from .. import recommendation_revalidations
        recommendation_revalidations.install_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=31 AND name='shared_vibe_model_generations'").fetchone():
        vibe_model_schema.install_conditioning_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=32 AND name='bounded_embedding_basis_guards'").fetchone():
        from .. import embedding_files
        embedding_files.install_basis_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE name='durable_adaptive_preview_pages'").fetchone():
        from .. import group_preview_work
        group_preview_work.install_guards(conn)


def _install_policy_history_capture(conn, context):
    from .. import policy_history
    policy_history.install_capture(conn,context)


def _validate_policy_history_capture(conn):
    from .. import policy_history
    policy_history.validate_capture(conn)


def _install_embedding_order(conn, context):
    from .. import embedding_files
    embedding_files.install_legacy_order(conn,context)


def _validate_embedding_order(conn):
    from .. import embedding_files
    embedding_files.validate_legacy_order(conn)


def _install_group_checkpoint_retention(conn, context):
    from .. import group_checkpoint_retention
    group_checkpoint_retention.install(conn, context)


def _validate_group_checkpoint_retention(conn):
    from .. import group_checkpoint_retention
    group_checkpoint_retention.validate(conn)


def _install_collection_recovery_epoch(conn, context):
    from .. import collection_recovery_epoch
    collection_recovery_epoch.install(conn, context)


def _validate_collection_recovery_epoch(conn):
    from .. import collection_recovery_epoch
    collection_recovery_epoch.validate(conn)


def _install_collection_recovery_keys(conn, context):
    from .. import collection_recovery_epoch
    collection_recovery_epoch.install_keys(conn, context)


def _validate_collection_recovery_keys(conn):
    from .. import collection_recovery_epoch
    collection_recovery_epoch.validate_keys(conn)


def _validate_collection_receipts(conn):
    if not sync_recovery_storage.folded(conn):
        file_receipts.collection.validate(conn)


def _install_recommendation_revalidations(conn, context):
    from .. import recommendation_revalidations
    recommendation_revalidations.install(conn, context)


def _validate_recommendation_revalidations(conn):
    from .. import recommendation_revalidations
    recommendation_revalidations.validate(conn)


def _install_embedding_basis_guards(conn, context):
    from .. import embedding_files
    embedding_files.install_basis_guards(conn, context)


def _validate_embedding_basis_guards(conn):
    from .. import embedding_files
    embedding_files.validate_basis_guards(conn)


def _install_tv_launches(conn, context):
    from . import initial_tv_launches
    initial_tv_launches.install(conn, context)


def _validate_tv_launches(conn):
    from . import initial_tv_launches
    initial_tv_launches.validate(conn)


def _install_association_work(conn, context):
    from .. import association_work
    association_work.install(conn, context)


def _validate_association_work(conn):
    from .. import association_work
    association_work.validate(conn)


def _install_group_preview_work(conn, context):
    from .. import group_preview_work
    group_preview_work.install(conn, context)


def _validate_group_preview_work(conn):
    from .. import group_preview_work
    group_preview_work.validate(conn)


def _install_song_library(conn, context):
    from .. import song_library
    song_library.install(conn)


def _validate_song_library(conn):
    from .. import song_library
    song_library.validate(conn)


def _install_effective_catalog_metadata(conn, context):
    from .. import group_preview_work

    catalog_metadata.install(conn, context)
    group_preview_work.install_guards(conn, sources={'catalog_entries'})


def _validate_effective_catalog_metadata(conn):
    catalog_metadata.validate(conn, canonical=recording_metadata.uses_canonical_view(conn))


def _install_provider_credits(conn, context):
    from .. import provider_credit_fields
    provider_credit_fields.install(conn)


def _validate_provider_credits(conn):
    from .. import provider_credit_fields
    provider_credit_fields.validate(conn)


def _retire_playlist_dismissals(conn, context):
    conn.execute('DROP TABLE legacy_playlist_dismissals')


def _validate_retired_playlist_dismissals(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='legacy_playlist_dismissals'").fetchone():
        raise MigrationError('Obsolete playlist dismissal storage is still installed')


_RETIRED_SNAPSHOTS = ('genre_classification_legacy', 'taste_axis_legacy', 'unresolved_legacy_embeddings')


def _retire_conversion_snapshots(conn, _context):
    for table in _RETIRED_SNAPSHOTS:
        conn.execute(f'DROP TABLE {table}')


def _validate_retired_conversion_snapshots(conn):
    row = conn.execute('SELECT name FROM main.sqlite_schema WHERE name IN (?,?,?)', _RETIRED_SNAPSHOTS).fetchone()
    if row:
        raise MigrationError('Obsolete conversion snapshot is still installed: ' + row[0])


_ALBUM_FEEDBACK_RESPONSES = "response IN ('more','known','reject','clear')"


def _fold_album_feedback_dismissals(conn, _context):
    sql = conn.execute("SELECT sql FROM main.sqlite_schema WHERE name='album_feedback'").fetchone()[0]
    sql = sql.replace("response IN ('more','known','reject')", _ALBUM_FEEDBACK_RESPONSES)
    constraint = ("CHECK(response<>'clear' OR (account_id IS NOT NULL AND withdrawn_at IS created_at "
                  'AND opportunity_id IS NULL AND action_id IS NULL))')
    sql = sql[:-1] + ',' + constraint + ')'
    indexes = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='index' AND tbl_name='album_feedback' "
                           'AND sql IS NOT NULL').fetchall()
    track_source_cutover._replace_table(conn, 'album_feedback', sql)
    for index, in indexes:
        conn.execute(index)
    conn.execute('''
        INSERT INTO album_feedback(account_id,album_id,source_browse_id,response,created_at,withdrawn_at)
        SELECT d.account_id,d.album_id,
               coalesce((SELECT min(p.provider_id) FROM album_provider_ids p
                         WHERE p.album_id=d.album_id AND p.provider='youtube'),''),
               'clear',d.dismissed_at,d.dismissed_at
        FROM album_feedback_legacy_dismissals d
        WHERE NOT EXISTS (SELECT 1 FROM album_feedback f WHERE f.account_id=d.account_id AND f.album_id=d.album_id)
    ''')
    conn.execute('DROP TABLE album_feedback_legacy_dismissals')


def _validate_folded_album_feedback_dismissals(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='album_feedback_legacy_dismissals'").fetchone():
        raise MigrationError('Obsolete album feedback dismissal storage is still installed')


def _compact_audio_evidence(conn, context):
    annotation_storage.install(conn, context)
    acousticbrainz_storage.install(conn)


def _validate_audio_evidence(conn):
    annotation_storage.validate(conn)
    acousticbrainz_storage.validate(conn)


def _correct_martin_jarl_name(conn, _context):
    # The provider identity names the solo artist. The collaboration label
    # remains in the original credit evidence and aliases.
    conn.execute("""
        UPDATE artists SET name='Martin Jarl'
        WHERE id=(
            SELECT artist_id FROM artist_provider_ids
            WHERE provider='musicbrainz' AND provider_id='9428c3b5-dbd3-4897-bae9-3e35536949ac'
        ) AND provisional=0 AND redirect_artist_id IS NULL AND name='Martin Jarl & Phelian'
    """)


CHANGES = (
    Change(1, 'independent_playback_feedback_receipts', _install_receipts, validate_receipts),
    Change(2, 'group_preparation_failure_progress', _install_group_progress, _validate_group_progress),
    Change(3, 'retired_genre_choice_capture', _retire_genre_choice_capture, _validate_retired_genre_capture),
    Change(4, 'retired_taste_axis_capture', _retire_taste_capture, _validate_retired_taste_capture),
    Change(5, 'retired_metadata_capture', _retire_metadata_capture, _validate_retired_metadata_capture),
    Change(6, 'resumable_group_retirement', _install_group_retirement, _validate_group_retirement),
    Change(7, 'bounded_installed_genre_repair', _repair_installed_genre, _validate_installed_genre),
    Change(8, 'installed_taste_guard_lifecycle', _repair_installed_genre, _validate_installed_genre_guards),
    Change(9, 'runtime_metadata_file_storage', _install_runtime_metadata, _validate_runtime_metadata),
    Change(10, 'compact_served_facts', _install_served_facts, _validate_served_facts),
    Change(11, 'displayed_mix_choice_descriptors', _install_mix_choices, _validate_mix_choices),
    Change(12, 'compact_artist_delivery_replays', _install_credit_retention, _validate_credit_retention),
    Change(13, 'retired_taste_mode_payloads', _install_mode_retention, _validate_mode_retention),
    Change(14, 'native_metadata_delivery_freshness', _install_native_metadata_heads, _validate_native_metadata_heads),
    Change(15, 'file_owned_prediction_provenance', _install_vibe_provenance, _validate_vibe_provenance),
    Change(16, 'file_owned_vibe_guards', _install_vibe_file_guards, _validate_vibe_file_guards),
    Change(17, 'monotonic_vibe_calculation_ids', _install_vibe_allocation, _validate_vibe_allocation),
    Change(18, 'file_owned_musical_profiles', _install_profile_files, _validate_profile_files),
    Change(19, 'file_owned_embeddings', _install_embedding_files, _validate_embedding_files),
    Change(20, 'file_owned_legacy_journals', _install_legacy_journals, _validate_legacy_journals),
    Change(21, 'protected_file_provenance_inputs', _install_provenance_lifecycle, _validate_provenance_lifecycle),
    Change(22, 'compact_historical_policy_identities', _install_policy_history, _validate_policy_history),
    Change(23, 'vibe_version_identities', _install_vibe_versions, _validate_vibe_versions),
    Change(24, 'continuing_historical_policy_attribution', _install_policy_history_capture, _validate_policy_history_capture),
    Change(25, 'continuing_installed_embedding_order', _install_embedding_order, _validate_embedding_order),
    Change(26, 'grouping_checkpoint_retention', _install_group_checkpoint_retention, _validate_group_checkpoint_retention),
    Change(27, 'collection_recovery_ownership_epoch', _install_collection_recovery_epoch, _validate_collection_recovery_epoch),
    Change(28, 'collection_recovery_scan_keys', _install_collection_recovery_keys, _validate_collection_recovery_keys),
    Change(29, 'revalidated_recommendation_pools', _install_recommendation_revalidations, _validate_recommendation_revalidations),
    Change(30, 'durable_album_feedback_responses', album_feedback_effects.install_outbox,
           album_feedback_effects.validate_outbox),
    Change(31, 'shared_vibe_model_generations', vibe_model_schema.install, vibe_model_schema.validate),
    Change(32, 'bounded_embedding_basis_guards', _install_embedding_basis_guards, _validate_embedding_basis_guards),
    Change(33, 'durable_initial_tv_launches', _install_tv_launches, _validate_tv_launches),
    Change(34, 'masked_vibe_metadata_guards', vibe_model_schema.install_metadata_guards,
           vibe_model_schema.validate_metadata_guards),
    Change(35, 'resumable_recording_associations', _install_association_work, _validate_association_work),
    Change(36, 'durable_home_revisit_progress', held_recommendation_state.install_revisit_progress,
           held_recommendation_state.validate_revisit_progress),
    Change(37, 'durable_adaptive_preview_pages', _install_group_preview_work, _validate_group_preview_work),
    Change(38, 'native_song_library_and_starter_picks', _install_song_library, _validate_song_library),
    Change(39, 'record_owned_audio_sample_files', file_receipts.install_sample_files, file_receipts.validate_sample_files),
    Change(40, 'record_owned_collection_documents', file_receipts.collection.install, _validate_collection_receipts),
    Change(41, 'record_owned_recommendation_files', file_receipts.recommendation.install, file_receipts.recommendation.validate),
    Change(42, 'record_owned_action_backups', file_receipts.action_backup.install, file_receipts.action_backup.validate),
    Change(43, 'record_owned_annotation_files', file_receipts.annotation.install, file_receipts.annotation.validate),
    Change(44, 'record_owned_playlist_captures', file_receipts.playlist_backup.install, file_receipts.playlist_backup.validate),
    Change(45, 'record_owned_policy_files', file_receipts.policy.install, file_receipts.policy.validate),
    Change(46, 'record_owned_policy_history', file_receipts.install_policy_history, file_receipts.policy_history.validate),
    Change(47, 'record_owned_embedding_files', file_receipts.install_embedding, file_receipts.validate_embedding),
    Change(48, 'record_owned_group_files', file_receipts.group.install, file_receipts.group.validate),
    Change(49, 'record_owned_profile_files', file_receipts.install_profile, file_receipts.validate_profile),
    Change(50, 'record_owned_vibe_model_files', file_receipts.vibe_model.install, file_receipts.vibe_model.validate),
    Change(51, 'record_owned_prediction_provenance', file_receipts.install_provenance, file_receipts.validate_provenance),
    Change(52, 'record_owned_paid_description_files', file_receipts.description.install, file_receipts.description.validate),
    Change(53, 'record_owned_runtime_metadata', file_receipts.metadata.install, file_receipts.metadata.validate),
    Change(54, 'record_owned_metadata_deliveries', file_receipts.delivery.install, file_receipts.delivery.validate),
    Change(55, 'record_owned_listening_evidence', file_receipts.evidence.install, file_receipts.evidence.validate),
    Change(56, 'pending_served_diagnostic_files', file_receipts.impression.install, file_receipts.impression.validate),
    Change(57, 'structured_provider_attempt_credits', _install_provider_credits, _validate_provider_credits),
    Change(58, 'dated_listener_model_documents', listener_state.install, listener_state.validate),
    Change(59, 'retained_choice_attribution', choice_learning_storage.install_attribution,
           choice_learning_storage.validate_attribution),
    Change(60, 'vibe_application_relations', vibe_versions.install_application_relations, vibe_versions.validate_application_relations),
    Change(61, 'metadata_delivery_application_relations', metadata_locator_contract.install_application_relations,
           metadata_locator_contract.validate_application_relations),
    Change(62, 'library_taste_summary', library_taste_summary.install, library_taste_summary.validate),
    Change(63, 'effective_catalog_metadata', _install_effective_catalog_metadata, _validate_effective_catalog_metadata),
    Change(64, 'prepared_input_write_relations', prepared_input_schema.install, prepared_input_schema.validate),
    Change(65, 'acousticbrainz_vibe_estimates', acousticbrainz_vibe_schema.install, acousticbrainz_vibe_schema.validate),
    Change(66, 'effective_recording_metadata', recording_metadata.install, recording_metadata.validate),
    Change(67, 'verified_acousticbrainz_vibe_matches', acousticbrainz_vibe_schema.install_verification,
           acousticbrainz_vibe_schema.validate_verification),
    Change(68, 'acousticbrainz_selected_recording_matches', acousticbrainz_vibe_schema.install_selected_recording,
           acousticbrainz_vibe_schema.validate_selected_recording),
    Change(69, 'unified_track_source_compatibility', track_sources.install, track_sources.validate),
    Change(70, 'indexed_acousticbrainz_vibe_matches', acousticbrainz_vibe_schema.install_match_index,
           acousticbrainz_vibe_schema.validate_match_index),
    Change(71, 'vibe_score_definitions', vibe_definitions.install, vibe_definitions.validate),
    Change(72, library_scope_changes.NAME, library_scope_changes.install, library_scope_changes.validate),
    Change(73, 'adaptive_preview_catalog_scope', group_preview_work.install_catalog_scope_guards,
           group_preview_work.validate_catalog_scope_guards),
    Change(74, 'fold_track_sources', track_source_cutover.install, track_source_cutover.validate,
           replace_tables=True, prepare=track_source_cutover.prepare),
    Change(75, recording_library.NAME, recording_library.install, recording_library.validate),
    Change(76, recording_library.DISCOVERY_NAME, recording_library.install_discovery,
           recording_library.validate_discovery),
    Change(77, 'retire_playlist_dismissals', _retire_playlist_dismissals,
           _validate_retired_playlist_dismissals),
    Change(78, 'retire_conversion_snapshots', _retire_conversion_snapshots, _validate_retired_conversion_snapshots),
    Change(79, 'fold_album_feedback_dismissals', _fold_album_feedback_dismissals,
           _validate_folded_album_feedback_dismissals),
    Change(80, 'fold_embedding_order', embedding_files.fold_legacy_order, embedding_files.validate_folded_order),
    Change(81, 'retire_recording_metadata_progress', track_source_cutover.retire_recording_metadata_progress,
           track_source_cutover.validate_retired_recording_metadata_progress),
    Change(82, 'fold_earlier_discovery_offers', discovery_offer_history.fold_earlier_offers,
           discovery_offer_history.validate_folded_offers, replace_tables=True),
    Change(83, recording_selection.NAME, recording_selection.install, recording_selection.validate),
    Change(84, paid_usage.NAME, paid_usage.install, paid_usage.validate, replace_tables=True),
    Change(85, legacy_journals.FOLD_RETIREMENTS, legacy_journals.fold_retirements,
           legacy_journals.validate_folded_retirements),
    Change(86, legacy_journals.RETIRE_PACK_RECEIPTS, legacy_journals.retire_empty_receipts,
           legacy_journals.validate_file_references),
    Change(87, account_storage.NAME, account_storage.install, account_storage.validate,
           replace_tables=True, foreign_key_owners=account_storage.FK_OWNERS),
    Change(88, retired_catalog.NAME, retired_catalog.install, retired_catalog.validate,
           replace_tables=True, prepare=retired_catalog.prepare,
           foreign_key_owners=retired_catalog.METADATA),
    Change(89, action_cleanup.NAME, action_cleanup.install, action_cleanup.validate),
    Change(90, album_storage.NAME, album_storage.install, album_storage.validate,
           replace_tables=True, prepare=album_storage.prepare, foreign_key_owners=album_storage.FK_OWNERS),
    Change(91, artist_storage.NAME, artist_storage.install, artist_storage.validate),
    Change(92, genre_storage.NAME, genre_storage.install, genre_storage.validate),
    Change(93, group_version_storage.NAME, group_version_storage.install, group_version_storage.validate),
    Change(94, measurement_storage.NAME, measurement_storage.install, measurement_storage.validate),
    Change(95, group_publication_storage.NAME, group_publication_storage.install, group_publication_storage.validate,
           replace_tables=True, foreign_key_owners=group_publication_storage.FK_OWNERS),
    Change(96, play_event_storage.NAME, play_event_storage.install, play_event_storage.validate,
           replace_tables=True, foreign_key_owners=play_event_storage.FK_OWNERS,
           prepare=play_event_preparation.prepare),
    Change(97, annotation_storage.NAME, _compact_audio_evidence, _validate_audio_evidence,
           replace_tables=True, prepare=annotation_storage.prepare,
           foreign_key_owners=(annotation_storage.TABLE, *acousticbrainz_storage.FK_OWNERS)),
    Change(98, group_publication_storage.REPAIR_NAME, group_publication_storage.repair_group_publication_guard,
           group_publication_storage.validate_group_publication_guard,
           replace_tables=True, foreign_key_owners=('group_versions',)),
    Change(99, play_event_storage.PROJECTION_NAME, play_event_storage.project_inline_evidence,
           play_event_storage.validate_inline_evidence),
    Change(100, acousticbrainz_vibe_schema.INDEXED_VIEW_CHANGE, acousticbrainz_vibe_schema.install_indexed_linked_classifier_view,
           acousticbrainz_vibe_schema.validate_indexed_linked_classifier_view),
    Change(101, 'adaptive_preview_library_readiness', group_preview_work.install_library_readiness_guards,
           group_preview_work.validate_library_readiness_guards),
    Change(102, artist_details.NAME, artist_details.install, artist_details.validate,
           replace_tables=True, prepare=artist_details.prepare, foreign_key_owners=('artist_credits',)),
    Change(103, account_bindings.NAME, account_bindings.install, account_bindings.validate,
           replace_tables=True, foreign_key_owners=('playlists',)),
    Change(104, playlist_scope_cleanup.NAME, playlist_scope_cleanup.install, playlist_scope_cleanup.validate),
    Change(105, playlist_items_storage.NAME, playlist_items_storage.install, playlist_items_storage.validate,
           replace_tables=True, foreign_key_owners=('playlist_items',),
           prepare=playlist_items_storage.prepare),
    Change(106, choice_learning_storage.NAME, choice_learning_storage.install, choice_learning_storage.validate,
           replace_tables=True, foreign_key_owners=choice_learning_storage.FOREIGN_KEY_OWNERS),
    Change(107, album_feedback_effects.NAME, album_feedback_effects.install, album_feedback_effects.validate),
    Change(108, paid_access_settings.NAME, paid_access_settings.install, paid_access_settings.validate),
    Change(109, prediction_storage.NAME, prediction_storage.install, prediction_storage.validate,
           replace_tables=True, foreign_key_owners=('predictions',),
           prepare=prediction_storage.prepare),
    Change(110, discovery_checkpoints.NAME, discovery_checkpoints.install, discovery_checkpoints.validate,
           prepare=discovery_checkpoints.prepare),
    Change(111, discovery_tracks.NAME, discovery_tracks.install, discovery_tracks.validate),
    Change(112, discovery_artists.NAME, discovery_artists.install, discovery_artists.validate),
    Change(113, group_checkpoint_storage.NAME, group_checkpoint_storage.install, group_checkpoint_storage.validate,
           replace_tables=True, foreign_key_owners=(group_checkpoint_storage.TABLE,)),
    Change(114, radio_session_storage.NAME, radio_session_storage.install, radio_session_storage.validate),
    Change(115, home_storage.NAME, home_storage.install, home_storage.validate, replace_tables=True,
           foreign_key_owners=('home_genre_requests',)),
    Change(116, recommendation_storage.NAME, recommendation_storage.install, recommendation_storage.validate,
           replace_tables=True, foreign_key_owners=('recommendation_build_pools',)),
    Change(117, recommendation_storage.PREVIEW_REVISION, recommendation_storage.install_preview_revision,
           recommendation_storage.validate_preview_revision),
    Change(118, metadata_delivery_storage.NAME, metadata_delivery_storage.install, metadata_delivery_storage.validate,
           replace_tables=True, foreign_key_owners=metadata_delivery_storage.FK_OWNERS),
    Change(119, group_generation_storage.NAME, group_generation_storage.install, group_generation_storage.validate,
           replace_tables=True, foreign_key_owners=group_generation_storage.FK_OWNERS),
    Change(120, recommendation_candidate_storage.NAME, recommendation_candidate_storage.install,
           recommendation_candidate_storage.validate),
    Change(121, metadata_observation_storage.NAME, metadata_observation_storage.install,
           metadata_observation_storage.validate, replace_tables=True,
           foreign_key_owners=('recording_metadata_selections', 'metadata_conflicts', 'metadata_runtime_choices')),
    Change(122, recommendation_refresh_storage.NAME, recommendation_refresh_storage.install,
           recommendation_refresh_storage.validate),
    Change(123, mix_menu_storage.NAME, mix_menu_storage.install, mix_menu_storage.validate,
           replace_tables=True, foreign_key_owners=mix_menu_storage.OWNERS),
    Change(124, model_selection_storage.NAME, model_selection_storage.install, model_selection_storage.validate),
    Change(125, metadata_observation_storage.GUARD_REPAIR, metadata_observation_storage.repair_selection_guards,
           metadata_observation_storage.validate_selection_guards),
    Change(126, served_impression_storage.NAME, served_impression_storage.install,
           served_impression_storage.validate),
    Change(127, road_trip_storage.NAME, road_trip_storage.install, road_trip_storage.validate),
    Change(128, policy_archive_storage.NAME, policy_archive_storage.install, policy_archive_storage.validate,
           replace_tables=True, foreign_key_owners=(policy_archive_storage.HISTORY, policy_archive_storage.LINKS)),
    Change(129, recording_association_storage.NAME, recording_association_storage.install,
           recording_association_storage.validate),
    Change(130, embedding_storage.NAME, embedding_storage.install, embedding_storage.validate,
           replace_tables=True, foreign_key_owners=embedding_storage.HEADS, prepare=embedding_storage.prepare),
    Change(131, musical_source_queue_storage.NAME, musical_source_queue_storage.install,
           musical_source_queue_storage.validate),
    Change(132, recording_profile_storage.NAME, recording_profile_storage.install,
           recording_profile_storage.validate, prepare=recording_profile_storage.prepare),
    Change(133, recording_redirect_storage.NAME, recording_redirect_storage.install,
           recording_redirect_storage.validate),
    Change(134, taste_axis_storage.NAME, taste_axis_storage.install, taste_axis_storage.validate),
    Change(135, group_input_retention_storage.NAME, group_input_retention_storage.install,
           group_input_retention_storage.validate, replace_tables=True,
           foreign_key_owners=group_input_retention_storage.OWNERS),
    Change(136, musical_source_profile_storage.NAME, musical_source_profile_storage.install,
           musical_source_profile_storage.validate, prepare=musical_source_profile_storage.prepare),
    Change(137, vibe_preparation_storage.NAME, vibe_preparation_storage.install, vibe_preparation_storage.validate),
    Change(138, rebuild_history.NAME, rebuild_history.install, rebuild_history.validate),
    Change(139, 'correct_martin_jarl_name', _correct_martin_jarl_name, lambda _conn: None),
    Change(140, sync_cursor_storage.NAME, sync_cursor_storage.install, sync_cursor_storage.validate,
           replace_tables=True, foreign_key_owners=(sync_cursor_storage.TABLE,)),
    Change(141, radio_launch_storage.NAME, radio_launch_storage.install, radio_launch_storage.validate,
           replace_tables=True, foreign_key_owners=(radio_launch_storage.TABLE,)),
    Change(142, worker_checkpoints.NAME, worker_checkpoints.install, worker_checkpoints.validate,
           replace_tables=True, foreign_key_owners=worker_checkpoints.OWNERS),
    Change(143, paid_access_owner.NAME, paid_access_owner.install, paid_access_owner.validate),
    Change(144, mix_worker_storage.NAME, mix_worker_storage.install, mix_worker_storage.validate),
    Change(145, work_sequences.NAME, work_sequences.install, work_sequences.validate,
           replace_tables=True, foreign_key_owners=work_sequences.OWNERS),
    Change(146, held_recommendation_state.NAME, held_recommendation_state.install, held_recommendation_state.validate),
    Change(147, group_preview_evidence_storage.NAME, group_preview_evidence_storage.install,
           group_preview_evidence_storage.validate),
    Change(148, maintenance_clocks.NAME, maintenance_clocks.install, maintenance_clocks.validate),
    Change(149, vibe_worker_storage.NAME, vibe_worker_storage.install, vibe_worker_storage.validate),
    Change(150, history_worker_storage.NAME, history_worker_storage.install, history_worker_storage.validate,
           replace_tables=True, foreign_key_owners=(history_worker_storage.PREDICTIONS,)),
    Change(151, tv_draw_storage.NAME, tv_draw_storage.install, tv_draw_storage.validate),
    Change(152, policy_worker_storage.NAME, policy_worker_storage.install, policy_worker_storage.validate,
           replace_tables=True, foreign_key_owners=(policy_worker_storage.HISTORY,)),
    Change(153, startup_worker_storage.NAME, startup_worker_storage.install, startup_worker_storage.validate),
    Change(154, metadata_capture_storage.NAME, metadata_capture_storage.install, metadata_capture_storage.validate),
    Change(155, import_and_fit_receipts.NAME, import_and_fit_receipts.install, import_and_fit_receipts.validate),
    Change(156, playlist_merge_preferences.NAME, playlist_merge_preferences.install, playlist_merge_preferences.validate),
    Change(157, audio_playback_storage.NAME, audio_playback_storage.install, audio_playback_storage.validate,
           replace_tables=True, foreign_key_owners=(audio_playback_storage.TABLE, audio_playback_storage.RETIRED)),
    Change(158, sync_recovery_storage.NAME, sync_recovery_storage.install, sync_recovery_storage.validate,
           replace_tables=True, foreign_key_owners=sync_recovery_storage.OWNERS),
    Change(159, album_playback_storage.NAME, album_playback_storage.install, album_playback_storage.validate),
    Change(160, radio_policy_storage.NAME, radio_policy_storage.install, radio_policy_storage.validate),
    Change(161, tv_storage.NAME, tv_storage.install, tv_storage.validate,
           replace_tables=True, foreign_key_owners=tv_storage.OWNERS),
    Change(162, spotify_storage.NAME, spotify_storage.install, spotify_storage.validate),
    Change(163, paid_description_storage.NAME, paid_description_storage.install, paid_description_storage.validate,
           replace_tables=True, foreign_key_owners=(paid_description_storage.TABLE, paid_description_storage.RETIRED)),
    Change(164, account_binding_receipt.NAME, account_binding_receipt.install, account_binding_receipt.validate),
    Change(165, vibe_model_receipts.NAME, vibe_model_receipts.install, vibe_model_receipts.validate),
    Change(166, playlist_bookkeeping.NAME, playlist_bookkeeping.install, playlist_bookkeeping.validate,
           replace_tables=True, foreign_key_owners=playlist_bookkeeping.OWNERS),
    Change(167, metadata_conflict_storage.NAME, metadata_conflict_storage.install, metadata_conflict_storage.validate),
    Change(168, recommendation_membership.NAME, recommendation_membership.install, recommendation_membership.validate,
           replace_tables=True, foreign_key_owners=recommendation_membership.OWNERS),
    Change(169, prepared_input_storage.NAME, prepared_input_storage.install, prepared_input_storage.validate,
           replace_tables=True, foreign_key_owners=prepared_input_storage.OWNERS),
    Change(170, report_part_storage.NAME, report_part_storage.install, report_part_storage.validate),
    Change(171, recommendation_receipt_storage.NAME, recommendation_receipt_storage.install,
           recommendation_receipt_storage.validate),
    Change(172, group_evidence_references.NAME, group_evidence_references.install, group_evidence_references.validate,
           replace_tables=True, foreign_key_owners=group_evidence_references.OWNERS),
    Change(173, group_input_digests.NAME, group_input_digests.install, group_input_digests.validate,
           replace_tables=True, foreign_key_owners=group_input_digests.OWNERS),
    Change(174, wiki_cache_storage.NAME, wiki_cache_storage.install, wiki_cache_storage.validate),
    Change(175, group_versions_by_generation.NAME, group_versions_by_generation.install,
           group_versions_by_generation.validate, replace_tables=True, foreign_key_owners=group_versions_by_generation.OWNERS),
    Change(176, control_settings_storage.NAME, control_settings_storage.install, control_settings_storage.validate),
    Change(177, listener_output_storage.NAME, listener_output_storage.install, listener_output_storage.validate),
    Change(178, preparation_checkpoint_storage.NAME, preparation_checkpoint_storage.install,
           preparation_checkpoint_storage.validate),
    Change(179, playlist_document_storage.NAME, playlist_document_storage.install, playlist_document_storage.validate,
           replace_tables=True, foreign_key_owners=playlist_document_storage.OWNERS),
    Change(180, publication_manifests.NAME, publication_manifests.install, publication_manifests.validate),
    Change(181, choice_evidence_storage.NAME, choice_evidence_storage.install, choice_evidence_storage.validate,
           prepare=choice_evidence_storage.prepare),
    Change(182, shared_file_receipts.NAME, shared_file_receipts.install, shared_file_receipts.validate,
           replace_tables=True, foreign_key_owners=(shared_file_receipts.TABLE,), prepare=shared_file_receipts.prepare),
    Change(183, provider_field_storage.NAME, provider_field_storage.install, provider_field_storage.validate,
           prepare=provider_field_storage.prepare),
    Change(184, mood_taste_storage.NAME, mood_taste_storage.install, mood_taste_storage.validate),
    Change(185, radio_discovery_details.NAME, radio_discovery_details.install, radio_discovery_details.validate),
    Change(186, shared_file_receipts.STATE_CHANGE, shared_file_receipts.install_state, shared_file_receipts.validate_state),
    Change(187, tv_preparation_details.NAME, tv_preparation_details.install, tv_preparation_details.validate),
    Change(188, musical_observation_storage.NAME, musical_observation_storage.install,
           musical_observation_storage.validate, prepare=musical_observation_storage.prepare),
    Change(189, model_artifacts_storage.NAME, model_artifacts_storage.install, model_artifacts_storage.validate),
    Change(190, action_collection_storage.NAME, action_collection_storage.install, action_collection_storage.validate),
    Change(191, playback_preference_storage.NAME, playback_preference_storage.install,
           playback_preference_storage.validate),
    Change(192, prediction_storage.RECEIPT_LOOKUP, prediction_storage.install_receipt_lookup,
           prediction_storage.validate_receipt_lookup),
    Change(193, home_taste_settings.NAME, home_taste_settings.install, home_taste_settings.validate),
    Change(194, radio_tv_settings.NAME, radio_tv_settings.install, radio_tv_settings.validate),
    Change(195, tv_playlist_recovery.NAME, tv_playlist_recovery.install, tv_playlist_recovery.validate),
    Change(196, sync_settings.NAME, sync_settings.install, sync_settings.validate),
    Change(197, spotify_records_storage.NAME, spotify_records_storage.install, spotify_records_storage.validate),
    Change(198, choice_learned_settings.NAME, choice_learned_settings.install, choice_learned_settings.validate),
    Change(199, prepared_display_settings.NAME, prepared_display_settings.install, prepared_display_settings.validate),
    Change(200, content_work_settings.NAME, content_work_settings.install, content_work_settings.validate),
    Change(201, recommendation_build_state.NAME, recommendation_build_state.install, recommendation_build_state.validate),
    Change(202, recommendation_custody.NAME, recommendation_custody.install, recommendation_custody.validate),
    Change(203, metadata_hint_storage.NAME, metadata_hint_storage.install, metadata_hint_storage.validate),
    Change(204, artist_credit_replay_storage.NAME, artist_credit_replay_storage.install, artist_credit_replay_storage.validate),
    Change(205, metadata_choice_storage.NAME, metadata_choice_storage.install, metadata_choice_storage.validate),
    Change(206, track_association_history.NAME, track_association_history.install, track_association_history.validate),
    Change(207, playback_binding_storage.NAME, playback_binding_storage.install, playback_binding_storage.validate),
    Change(208, experiment_decision_storage.NAME, experiment_decision_storage.install, experiment_decision_storage.validate),
    Change(209, discovery_impression_storage.NAME, discovery_impression_storage.install, discovery_impression_storage.validate),
    Change(210, recording_content_work.NAME, recording_content_work.install, recording_content_work.validate),
    Change(211, prepared_display_files.NAME, prepared_display_files.install, prepared_display_files.validate),
    Change(212, home_recommendation_roots.NAME, home_recommendation_roots.install, home_recommendation_roots.validate),
    Change(213, group_context_owners.NAME, group_context_owners.install, group_context_owners.validate,
           replace_tables=True, foreign_key_owners=group_context_owners.OWNERS),
    Change(214, metadata_selection_owners.NAME, metadata_selection_owners.install, metadata_selection_owners.validate,
           prepare=metadata_selection_owners.prepare),
    Change(215, runtime_choice_owners.NAME, runtime_choice_owners.install, runtime_choice_owners.validate),
    Change(216, metadata_history_owners.NAME, metadata_history_owners.install, metadata_history_owners.validate,
           prepare=metadata_history_owners.prepare),
    Change(217, recommendation_chunk_receipts.NAME, recommendation_chunk_receipts.install, recommendation_chunk_receipts.validate,
           prepare=recommendation_chunk_receipts.prepare),
    Change(218, playback_response_receipts.NAME, playback_response_receipts.install, playback_response_receipts.validate,
           prepare=playback_response_receipts.prepare),
    Change(219, recording_library.LISTENING_PROBES_NAME, recording_library.install_listening_probes,
           recording_library.validate_listening_probes),
    Change(220, rating_action_events.NAME, rating_action_events.install, rating_action_events.validate),
    Change(221, recommendation_impression_counts.NAME, recommendation_impression_counts.install,
           recommendation_impression_counts.validate),
    Change(222, taste_axis_graduations.NAME, taste_axis_graduations.install, taste_axis_graduations.validate),
    Change(223, mode_offer_settings.NAME, mode_offer_settings.install, mode_offer_settings.validate),
    Change(224, policy_body_settings.NAME, policy_body_settings.install, policy_body_settings.validate,
           replace_tables=True, foreign_key_owners=(policy_body_settings.VARIANTS,)),
    Change(225, policy_history_settings.NAME, policy_history_settings.install, policy_history_settings.validate,
           replace_tables=True, foreign_key_owners=(policy_history_settings.IMPRESSIONS,)),
    Change(226, group_evidence_settings.NAME, group_evidence_settings.install, group_evidence_settings.validate),
    Change(227, legacy_mode_storage.NAME, legacy_mode_storage.install, legacy_mode_storage.validate,
           True, foreign_key_owners=('playlists', 'taste_modes', 'taste_groups')),
)


def _applied(conn, changes):
    from .current import _tokens
    from ..schema_inspection import reuse_objects

    row = conn.execute("SELECT type,sql FROM main.sqlite_schema WHERE name='schema_changes'").fetchone()
    if row is None:
        return 0
    if row[0] != 'table' or _tokens(row[1]) != _tokens(HISTORY_SQL):
        raise MigrationError('Incompatible schema_changes layout')
    rows = conn.execute('SELECT version,name,applied_at FROM schema_changes ORDER BY version').fetchall()
    if len(rows) > len(changes):
        raise MigrationError('Regular schema changes are newer than this application')
    unified = track_sources.folded(conn)
    generation_versions = any(row[1] == group_versions_by_generation.NAME for row in rows)
    control_settings = any(row[1] == control_settings_storage.NAME for row in rows)
    shared_receipts = any(row[1] == shared_file_receipts.NAME for row in rows)
    inline_tv = any(row[1] == tv_preparation_details.NAME for row in rows)
    inline_estimates = any(row[1] == model_artifacts_storage.NAME for row in rows)
    home_settings = any(row[1] == home_taste_settings.NAME for row in rows)
    radio_tv = any(row[1] == radio_tv_settings.NAME for row in rows)
    tv_recovery = any(row[1] == tv_playlist_recovery.NAME for row in rows)
    sync_owned = any(row[1] == sync_settings.NAME for row in rows)
    spotify_owned = any(row[1] == spotify_records_storage.NAME for row in rows)
    choice_settings = any(row[1] == choice_learned_settings.NAME for row in rows)
    display_settings = any(row[1] == prepared_display_settings.NAME for row in rows)
    recording_content = any(row[1] == recording_content_work.NAME for row in rows)
    policy_archives = any(row[1] == policy_history_settings.NAME for row in rows)
    group_context_settings = any(row[1] == group_evidence_settings.NAME for row in rows)
    modes_folded = any(row[1] == legacy_mode_storage.NAME for row in rows)
    with reuse_objects(conn):
        for row, change in zip(rows, changes):
            if row[0] != change.version or row[1] != change.name or not isinstance(row[2], (int, float)):
                raise MigrationError('Regular schema history is not the supported contiguous prefix')
            # The unified destination validates the replaced owners together.
            # Older callbacks describe constraints against the previous tables.
            if inline_estimates and change.version == 97:
                annotation_storage.validate(conn)
                continue
            if home_settings and change.name == mix_worker_storage.NAME:
                mix_worker_storage.validate(conn, home_settings=True)
                continue
            if home_settings and change.name == mood_taste_storage.NAME:
                mood_taste_storage.validate(conn, taste_settings=True)
                continue
            if radio_tv and change.name == tv_preparation_details.NAME:
                tv_preparation_details.validate(conn, preparation_settings=True, playlist_recovery=tv_recovery)
                continue
            if radio_tv and change.name == radio_discovery_details.NAME:
                radio_discovery_details.validate(conn, seed_setting=True)
                continue
            if sync_owned and change.name == import_and_fit_receipts.NAME:
                import_and_fit_receipts.validate(conn, sync_settings=True)
                continue
            if display_settings and change.name == home_storage.NAME:
                home_storage.validate(conn, display_settings=True)
                continue
            if display_settings and change.name == publication_manifests.NAME:
                publication_manifests.validate(conn, display_settings=True)
                continue
            if display_settings and change.name == report_part_storage.NAME:
                report_part_storage.validate(conn, display_settings=True)
                continue
            if modes_folded and change.name == control_settings_storage.NAME:
                control_settings_storage.validate(conn, modes_folded=True)
                continue
            if ((not unified or change.version >= 68) and not
                    (generation_versions and change.version in group_versions_by_generation.SUPERSEDED_VALIDATIONS)
                    and not (control_settings and change.version in (142, 148, 150))
                    and not (shared_receipts and change.version in (118, 146, 158))
                    and not (sync_owned and change.name == sync_cursor_storage.NAME)
                    and not (spotify_owned and change.name == spotify_storage.NAME)
                    and not (recording_content and change.name == content_work_settings.NAME)
                    and not (choice_settings and change.name == choice_learning_storage.NAME)
                    and not (policy_archives and change.name in (policy_archive_storage.NAME, policy_worker_storage.NAME))
                    and not (group_context_settings and change.name in (group_evidence_references.NAME,
                             group_context_owners.NAME, group_preview_evidence_storage.NAME))
                    and not (inline_tv and change.name == tv_storage.NAME)
                    and not (radio_tv and change.name == tv_draw_storage.NAME)
                    and not (inline_estimates and change.version in (68, 100))):
                change.validate(conn)
    return len(rows)


def validate(conn):
    """Read-only admission check; does not install pending changes or scan data."""
    from .. import group_progress
    if group_progress.available(conn):
        group_progress.validate(conn)
    return _applied(conn, CHANGES)


def migrate(conn, *, changes=None, clock=time.time):
    """Apply pending schema changes in order, with one transaction per change."""
    from ..schema_inspection import reuse_tables

    changes = CHANGES if changes is None else tuple(changes)
    if ([change.version for change in changes] != list(range(1, len(changes) + 1))
            or len({change.name for change in changes}) != len(changes)):
        raise MigrationError('Invalid regular schema registry')
    if conn.in_transaction:
        raise MigrationError('Cannot change schema with an outstanding caller transaction')
    finish_source_table_upgrade(conn, changes=changes, clock=clock)
    with reuse_tables(conn) as inspections:
        _migrate(conn, changes, clock, inspections)


def finish_source_table_upgrade(conn, *, changes=None, clock=time.time):
    if not library_scope_changes.preview_layout(conn):
        return
    from .registry import MIGRATIONS
    from .runner import history_table

    changes = CHANGES if changes is None else changes
    try:
        conn.execute('BEGIN IMMEDIATE')
        if library_scope_changes.preview_layout(conn):
            applied = history_table(conn, 'schema_changes', changes)
            if (history(conn, MIGRATIONS) != 107 or applied != 71 or len(changes) <= applied
                    or changes[applied].name != library_scope_changes.NAME):
                raise MigrationError('Unsupported intermediate track source schema history')
            library_scope_changes.upgrade_preview(
                conn, metadata_active=control_settings_storage.predecessor_metadata_active(conn),
                metadata_definitions=retired_catalog.predecessor_definitions(),
            )
            conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                         (changes[applied].version, changes[applied].name, clock()))
            validate_version(conn, 107, foreign_keys=False, data=False)
            _applied(conn, changes)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _validated_history(conn, changes):
    from .registry import MIGRATIONS
    from ..schema_inspection import reuse_objects

    # Both validations inspect the same pinned schema. Discard the catalog
    # before applying DDL or leaving this transaction.
    with reuse_objects(conn):
        version = history(conn, MIGRATIONS)
        if version != 107:
            raise MigrationError('Regular schema changes require the current release baseline')
        validate_version(conn, version, foreign_keys=False, data=False)
        applied = _applied(conn, changes)
        from .runner import RELEASE_CHANGES
        if applied < RELEASE_CHANGES:
            raise MigrationError('Regular schema changes require the current release baseline')
        return applied


def _foreign_key_violation(conn, owners):
    if not owners:
        return conn.execute('PRAGMA foreign_key_check').fetchone()
    params = ','.join('?' for _ in owners)
    # Check surviving rows owned by the migration and every table that
    # references one of its owners, including any owner it dropped.
    return conn.execute(f'''
        WITH scope AS MATERIALIZED (
            SELECT DISTINCT s.name FROM main.sqlite_schema s JOIN pragma_foreign_key_list(s.name) f
            WHERE s.type='table' AND (s.name IN ({params}) OR f."table" IN ({params}))
        )
        SELECT 1 FROM scope JOIN pragma_foreign_key_check(scope.name) LIMIT 1
    ''', (*owners, *owners)).fetchone()


def _migrate(conn, changes, clock, inspections):
    from .runner import _release_changes

    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    legacy_alter = conn.execute('PRAGMA legacy_alter_table').fetchone()[0]
    admitted = None
    while True:
        planned = None
        replacing = False
        try:
            conn.execute('BEGIN')
            inspections.refresh()
            planned = _release_changes(conn, changes)
            if planned == len(changes):
                _validated_history(conn, changes)
                conn.rollback()
                return
            change = changes[planned]
            if change.prepare is not None and admitted != (planned, inspections.version):
                _validated_history(conn, changes)
            conn.rollback()
            prepared = None
            if change.prepare is not None:
                admitted = None
                prepared = change.prepare(conn, change.version)
            replacing = change.replace_tables
            if replacing:
                if foreign_keys:
                    conn.execute('PRAGMA foreign_keys=OFF')
                if not legacy_alter:
                    conn.execute('PRAGMA legacy_alter_table=ON')
            conn.execute('BEGIN IMMEDIATE')
            # A competing startup may have committed while this caller waited.
            # Check the ledger before validating a layout this caller cannot use.
            if _release_changes(conn, changes) != planned:
                conn.rollback()
                continue
            inspections.refresh()
            # Each successful change validates its own output. Re-admit the
            # entire layout after another connection commits, or after this
            # connection has run preparation outside the DDL transaction.
            if admitted != (planned, inspections.version):
                _validated_history(conn, changes)
            change.apply(conn, Context(clock(), prepared))
            if not conn.in_transaction:
                raise MigrationError('Regular schema change committed outside the runner')
            inspections.refresh()
            change.validate(conn)
            conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                         (change.version, change.name, clock()))
            if change.replace_tables:
                validate_version(conn, 107, foreign_keys=False, data=False)
                if _foreign_key_violation(conn, change.foreign_key_owners):
                    raise MigrationError('Invalid foreign-key reference after ' + change.name)
            conn.commit()
            admitted = (change.version, inspections.version)
        except PreparationChanged:
            conn.rollback()
        except sqlite3.OperationalError as error:
            conn.rollback()
            # A peer can finish several changes before SQLite grants this
            # connection a write transaction. An expired reservation for an
            # already installed change is harmless. Retry only on progress.
            if (getattr(error, 'sqlite_errorcode', 0) & 255 != sqlite3.SQLITE_BUSY
                    or planned is None or _release_changes(conn, changes) <= planned):
                raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            # Setting foreign_keys expires SQLite's prepared statements even
            # when the setting already has that value.
            if replacing:
                if foreign_keys:
                    conn.execute('PRAGMA foreign_keys=ON')
                if not legacy_alter:
                    conn.execute('PRAGMA legacy_alter_table=OFF')
