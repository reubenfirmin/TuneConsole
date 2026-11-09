"""Bounded additive schema changes, independent of the historical layout chain.

schema_migrations retains the exact already-applied layout/conversion history.
This ledger records only completed regular changes. It never records a pending
legacy conversion as applied. Bulk preparation must not be registered here.
"""
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from .runner import Context, MigrationError, history, validate_version


HISTORY_SQL = ('CREATE TABLE schema_changes (version INTEGER PRIMARY KEY,'
               'name TEXT NOT NULL,applied_at REAL NOT NULL)')


@dataclass(frozen=True)
class Change:
    version: int
    name: str
    apply: Callable
    validate: Callable


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
    if (conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone()
            and conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name GLOB 'prepare_genre_choices_dependency_*'").fetchone()):
        from .v085_genre_preferences import retire_preparation_capture
        retire_preparation_capture(conn)


def _validate_retired_genre_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone():
        names = [f'prepare_genre_choices_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')', names).fetchone():
            raise MigrationError('Retired genre choice capture still references its removed source')


def _retire_taste_capture(conn, context):
    if (conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone()
            and conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name GLOB 'prepare_taste_axes_dependency_*'").fetchone()):
        from .v088_taste_axes import retire_preparation_capture
        retire_preparation_capture(conn)


def _validate_retired_taste_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone():
        names=[f'prepare_taste_axes_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias','genre_family') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')',names).fetchone():
            raise MigrationError('Retired taste capture still references its removed keys')


def _retire_metadata_capture(conn, context):
    if (conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_observations'").fetchone()
            and conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name GLOB 'prepare_metadata_dependency_*' "
                             "OR name GLOB 'prepare_metadata_conflicts_dependency_*'").fetchone()):
        from .v080_metadata_evidence import retire_preparation_capture
        retire_preparation_capture(conn)


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
    from ..preparation import installed_genre_contract
    installed_genre_contract.install(conn,context)


def _validate_installed_genre(conn):
    from ..preparation import installed_genre_contract
    # Change 7 could precede the taste owner. Change 8 repairs its known missing
    # guards and establishes strict validation without rewriting applied rows.
    installed_genre_contract.validate(conn, allow_missing_guards=True)


def _validate_installed_genre_guards(conn):
    from ..preparation import installed_genre_contract
    installed_genre_contract.validate(conn)


def _install_runtime_metadata(conn, context):
    from .. import metadata_runtime
    metadata_runtime.install(conn, context)


def _validate_runtime_metadata(conn):
    from .. import metadata_runtime
    metadata_runtime.validate(conn)


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
    from .. import legacy_journals
    legacy_journals.install(conn, context)


def _validate_legacy_journals(conn):
    from .. import legacy_journals
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
        from ..preparation import installed_genre_contract
        installed_genre_contract.install_guards(conn)
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
        from yt_playlist.repos import vibe_models
        vibe_models.install_conditioning_guards(conn)
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=32 AND name='bounded_embedding_basis_guards'").fetchone():
        from .. import embedding_files
        embedding_files.install_basis_guards(conn)


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


def _install_recommendation_revalidations(conn, context):
    from .. import recommendation_revalidations
    recommendation_revalidations.install(conn, context)


def _validate_recommendation_revalidations(conn):
    from .. import recommendation_revalidations
    recommendation_revalidations.validate(conn)


def _install_album_feedback_outbox(conn, context):
    from .. import album_feedback_outbox
    album_feedback_outbox.install(conn, context)


def _validate_album_feedback_outbox(conn):
    from .. import album_feedback_outbox
    album_feedback_outbox.validate(conn)


def _install_joint_vibes(conn, context):
    from yt_playlist.repos import vibe_models
    vibe_models.install(conn, context)


def _validate_joint_vibes(conn):
    from yt_playlist.repos import vibe_models
    vibe_models.validate(conn)


def _install_embedding_basis_guards(conn, context):
    from .. import embedding_files
    embedding_files.install_basis_guards(conn, context)


def _validate_embedding_basis_guards(conn):
    from .. import embedding_files
    embedding_files.validate_basis_guards(conn)


def _install_tv_launches(conn, context):
    from .. import tv_launch_state
    tv_launch_state.install(conn, context)


def _validate_tv_launches(conn):
    from .. import tv_launch_state
    tv_launch_state.validate(conn)


def _install_joint_metadata_guards(conn, context):
    from yt_playlist.repos import vibe_models
    vibe_models.install_metadata_guards(conn, context)


def _validate_joint_metadata_guards(conn):
    from yt_playlist.repos import vibe_models
    vibe_models.validate_metadata_guards(conn)


def _install_association_work(conn, context):
    from .. import association_work
    association_work.install(conn, context)


def _validate_association_work(conn):
    from .. import association_work
    association_work.validate(conn)


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
    Change(30, 'durable_album_feedback_responses', _install_album_feedback_outbox, _validate_album_feedback_outbox),
    Change(31, 'shared_vibe_model_generations', _install_joint_vibes, _validate_joint_vibes),
    Change(32, 'bounded_embedding_basis_guards', _install_embedding_basis_guards, _validate_embedding_basis_guards),
    Change(33, 'durable_initial_tv_launches', _install_tv_launches, _validate_tv_launches),
    Change(34, 'masked_vibe_metadata_guards', _install_joint_metadata_guards, _validate_joint_metadata_guards),
    Change(35, 'resumable_recording_associations', _install_association_work, _validate_association_work),
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
    with reuse_objects(conn):
        for row, change in zip(rows, changes):
            if row[0] != change.version or row[1] != change.name or not isinstance(row[2], (int, float)):
                raise MigrationError('Regular schema history is not the supported contiguous prefix')
            change.validate(conn)
    return len(rows)


def validate(conn):
    """Read-only admission check; does not install pending changes or scan data."""
    from .. import group_progress
    if group_progress.available(conn):
        group_progress.validate(conn)
    from ..preparation import (
        action_publication, album_note_publication, album_publication, album_share_publication, append_publication, calendar_indexes, checkpoint_publication, choice_publication, clicked_publication, collection_document_publication, collection_feedback_publication, conflict_publication,
        control_publication, demand_publication, discovery_artist_publication, discovery_checkpoint_publication, discovery_publication, draft_publication, genre_preference_publication, genre_publication, home_publication, import_publication, library_sync_publication, listening_publication, model_publication,
        metadata_destinations, metadata_publication, offer_publication, pair_publication, playback_publication, radio_origin_publication,
        radio_publication, recipe_publication, report_publication, saved_output_publication,
        subject_publication, taste_control_publication, taste_publication, trial_publication, tv_publication, work_publication, workflow_publication,
    )
    calendar_indexes.validate_installed(conn)
    radio_publication.validate_installed(conn)
    radio_origin_publication.validate_installed(conn)
    saved_output_publication.validate_installed(conn)
    collection_feedback_publication.validate_installed(conn)
    playback_publication.validate_installed(conn)
    library_sync_publication.validate_installed(conn)
    listening_publication.validate_installed(conn)
    subject_publication.validate_installed(conn)
    discovery_checkpoint_publication.validate_installed(conn)
    discovery_artist_publication.validate_installed(conn)
    discovery_publication.validate_installed(conn)
    genre_publication.validate_installed(conn)
    genre_preference_publication.validate_installed(conn)
    taste_control_publication.validate_installed(conn)
    taste_publication.validate_installed(conn)
    import_publication.validate_installed(conn)
    tv_publication.validate_installed(conn)
    collection_document_publication.validate_installed(conn)
    draft_publication.validate_installed(conn)
    recipe_publication.validate_installed(conn)
    clicked_publication.validate_installed(conn)
    trial_publication.validate_installed(conn)
    pair_publication.validate_installed(conn)
    home_publication.validate_installed(conn)
    workflow_publication.validate_installed(conn)
    append_publication.validate_installed(conn)
    action_publication.validate_installed(conn)
    album_note_publication.validate_installed(conn)
    album_publication.validate_installed(conn)
    album_share_publication.validate_installed(conn)
    work_publication.validate_installed(conn)
    offer_publication.validate_installed(conn)
    report_publication.validate_installed(conn)
    control_publication.validate_installed(conn)
    demand_publication.validate_installed(conn)
    checkpoint_publication.validate_installed(conn)
    choice_publication.validate_installed(conn)
    conflict_publication.validate_installed(conn)
    metadata_publication.validate_installed(conn)
    metadata_destinations.validate_installed(conn)
    model_publication.validate_installed(conn)
    return _applied(conn, CHANGES)


def migrate(conn, *, changes=None, clock=time.time):
    """Commit each bounded change separately, regardless of pending layout work."""
    from ..schema_inspection import reuse_tables

    changes = CHANGES if changes is None else tuple(changes)
    if ([change.version for change in changes] != list(range(1, len(changes) + 1))
            or len({change.name for change in changes}) != len(changes)):
        raise MigrationError('Invalid regular schema registry')
    if conn.in_transaction:
        raise MigrationError('Cannot change schema with an outstanding caller transaction')
    with reuse_tables(conn) as inspections:
        _migrate(conn, changes, clock, inspections)


def _validated_history(conn, changes):
    from .registry import MIGRATIONS
    from ..schema_inspection import reuse_objects

    # Both validations inspect the same pinned schema. Discard the catalog
    # before applying DDL or leaving this transaction.
    with reuse_objects(conn):
        version = history(conn, MIGRATIONS)
        if version < 79:
            raise MigrationError('Regular schema changes require a supported installed layout (79+)')
        validate_version(conn, version, foreign_keys=False, data=False)
        return _applied(conn, changes)


def _migrate(conn, changes, clock, inspections):

    pending = False
    while True:
        try:
            if not pending:
                conn.execute('BEGIN')
                inspections.refresh()
                applied = _validated_history(conn, changes)
                if applied == len(changes):
                    conn.rollback()
                    return
                conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            inspections.refresh()
            # Another startup may have advanced either ledger after the read
            # plan. Recheck under the reservation before applying bounded DDL.
            applied = _validated_history(conn, changes)
            if applied == len(changes):
                conn.rollback()
                return
            if applied == 0 and not conn.execute(
                    "SELECT 1 FROM main.sqlite_schema WHERE name='schema_changes'").fetchone():
                conn.execute(HISTORY_SQL)
            change = changes[applied]
            change.apply(conn, Context(clock()))
            if not conn.in_transaction:
                raise MigrationError('Regular schema change committed outside the runner')
            change.validate(conn)
            conn.execute('INSERT INTO schema_changes VALUES (?,?,?)',
                         (change.version, change.name, clock()))
            conn.commit()
            # Our completed change already establishes that another is due.
            # Recheck the live schema and both ledgers under the next write
            # reservation; a second read plan would repeat that same work.
            pending = applied + 1 < len(changes)
        except BaseException:
            conn.rollback()
            raise
