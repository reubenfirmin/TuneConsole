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
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone():
        from .v085_genre_preferences import retire_preparation_capture
        retire_preparation_capture(conn)


def _validate_retired_genre_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='genre_preferences'").fetchone():
        names = [f'prepare_genre_choices_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')', names).fetchone():
            raise MigrationError('Retired genre choice capture still references its removed source')


def _retire_taste_capture(conn, context):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone():
        from .v088_taste_axes import retire_preparation_capture
        retire_preparation_capture(conn)


def _validate_retired_taste_capture(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone():
        names=[f'prepare_taste_axes_dependency_{owner}_{operation}'
            for owner in ('genre_catalog','genre_alias','genre_family') for operation in ('insert','update','delete')]
        if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name IN ('+','.join('?' for _ in names)+')',names).fetchone():
            raise MigrationError('Retired taste capture still references its removed keys')


def _retire_metadata_capture(conn, context):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='metadata_observations'").fetchone():
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


def after_owner_upgrade(conn):
    """Keep adopted bounded guards when an explicit upgrade creates an owner."""
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='schema_changes'").fetchone():
        return
    if conn.execute("SELECT 1 FROM schema_changes WHERE version=7 AND name='bounded_installed_genre_repair'").fetchone():
        from ..preparation import installed_genre_contract
        installed_genre_contract.install_guards(conn)


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
)


def _applied(conn, changes):
    from .current import _tokens

    row = conn.execute("SELECT type,sql FROM main.sqlite_schema WHERE name='schema_changes'").fetchone()
    if row is None:
        return 0
    if row[0] != 'table' or _tokens(row[1]) != _tokens(HISTORY_SQL):
        raise MigrationError('Incompatible schema_changes layout')
    rows = conn.execute('SELECT version,name,applied_at FROM schema_changes ORDER BY version').fetchall()
    if len(rows) > len(changes):
        raise MigrationError('Regular schema changes are newer than this application')
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
    from .registry import MIGRATIONS

    changes = CHANGES if changes is None else tuple(changes)
    if ([change.version for change in changes] != list(range(1, len(changes) + 1))
            or len({change.name for change in changes}) != len(changes)):
        raise MigrationError('Invalid regular schema registry')
    if conn.in_transaction:
        raise MigrationError('Cannot change schema with an outstanding caller transaction')
    while True:
        try:
            conn.execute('BEGIN')
            version = history(conn, MIGRATIONS)
            if version < 79:
                raise MigrationError('Regular schema changes require a supported installed layout (79+)')
            validate_version(conn, version, foreign_keys=False, data=False)
            applied = _applied(conn, changes)
            if applied == len(changes):
                conn.rollback()
                return
            conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            # Another startup may have advanced either ledger after the read
            # plan. Recheck under the reservation before applying bounded DDL.
            version = history(conn, MIGRATIONS)
            if version < 79:
                raise MigrationError('Regular schema changes require a supported installed layout (79+)')
            validate_version(conn, version, foreign_keys=False, data=False)
            applied = _applied(conn, changes)
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
        except BaseException:
            conn.rollback()
            raise
