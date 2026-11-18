"""Exact regular-admission oracle for the installed v103/v107 fixtures."""
from contextlib import closing
import sqlite3

from yt_playlist.core import (
    album_feedback_outbox, association_work,
    collection_recovery_epoch, credit_retention, embedding_files, group_checkpoint_retention, group_progress, group_retention_progress,
    group_preview_work, home_revisit_progress, legacy_journals, metadata_native, metadata_runtime, mix_choices,
    mode_retention, policy_history, profile_files, recommendation_revalidations, served_facts, vibe_provenance,
    vibe_versions, tv_launch_state, song_library,
)
from yt_playlist.core.migrations import current, migrate, regular
from yt_playlist.core import genre_schema as installed_genre_contract
from yt_playlist.repos import metadata_file_writes, delivery_file_writes, evidence_file_writes, impression_file_writes
from yt_playlist.repos import action_backup_writes, annotation_file_writes, collection_file_writes, description_file_writes, embedding_file_writes, group_file_writes, playlist_backups, policy_file_writes, policy_history_files, profile_file_writes, provenance_file_writes, recommendation_file_writes, vibe_model_file_writes, vibe_models


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
    'resumable_recording_associations',
    'durable_home_revisit_progress',
    'durable_adaptive_preview_pages',
    'native_song_library_and_starter_picks',
    'record_owned_audio_sample_files',
    'record_owned_collection_documents',
    'record_owned_recommendation_files',
    'record_owned_action_backups',
    'record_owned_annotation_files',
    'record_owned_playlist_captures',
    'record_owned_policy_files',
    'record_owned_policy_history',
    'record_owned_embedding_files',
    'record_owned_group_files',
    'record_owned_profile_files',
    'record_owned_vibe_model_files',
    'record_owned_prediction_provenance',
    'record_owned_paid_description_files',
    'record_owned_runtime_metadata',
    'record_owned_metadata_deliveries',
    'record_owned_listening_evidence',
    'pending_served_diagnostic_files',
)


def register_independent_ddl(monkeypatch, conn):
    """Append a real pending change without replacing any applied history.

    Keep every production change, including the unapplied suffix. A disposable
    input must still run the complete registry before the added change.
    """
    regular.validate(conn)
    change = regular.Change(
        len(regular.CHANGES) + 1, 'independent_preparation_ddl',
        lambda conn, context: conn.execute('CREATE TABLE independent_preparation_ddl (id INTEGER)'),
        lambda conn: conn.execute('SELECT id FROM independent_preparation_ddl LIMIT 0'),
    )
    assert not conn.execute(
        "SELECT 1 FROM main.sqlite_schema WHERE name='independent_preparation_ddl'").fetchone()
    from tests.release_schema_support import future_schema
    future_schema(monkeypatch, change.name, 'CREATE TABLE independent_preparation_ddl (id INTEGER)')
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, change))
    return change


def captured(conn):
    return {name: sorted((tuple(row) for row in conn.execute('SELECT * FROM "'+name+'"')), key=repr)
            for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
