"""Repeated admission must inspect each installed database's current structure."""
from contextlib import ExitStack, closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, install_regular_input
from yt_playlist.core import (
    credit_retention, embedding_files, group_checkpoint_retention, group_progress,
    group_retention_progress, metadata_runtime, mix_choices, mode_retention,
    profile_files, served_facts, vibe_provenance,
)
from yt_playlist.core.migrations import current, regular, runner
from yt_playlist.core.migrations import (
    v061_recording_vibes, v067_audio_annotations, v068_canonical_embeddings,
    v070_policy_documents, v071_album_descriptions, v072_compact_groups,
    v073_recommendation_publications, v076_legacy_evidence_owners,
    v077_scope_account_state, v078_account_contracts,
)


@pytest.fixture(scope='module')
def installed_inputs():
    # Repeated admission owns real validation of private, mutable SQL. The
    # historical migration chain and genre catalog population are lower inputs.
    with ExitStack() as resources:
        templates = {}

        def template(version, *, completed_regular=False):
            key = version, completed_regular
            if key not in templates:
                conn = resources.enter_context(closing(sqlite3.connect(':memory:')))
                with pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch)
                    install_numbered_input(conn, version)
                    if completed_regular:
                        with conn:
                            conn.execute('BEGIN')
                            install_regular_input(conn, regular.CHANGES)
                conn.execute('PRAGMA query_only=ON')
                templates[key] = conn
            return templates[key]

        yield template


VALIDATORS = (
    v061_recording_vibes, v067_audio_annotations, v068_canonical_embeddings,
    v070_policy_documents, v071_album_descriptions, v072_compact_groups,
    v073_recommendation_publications, v076_legacy_evidence_owners,
    v077_scope_account_state, v078_account_contracts,
)


@pytest.mark.parametrize('version', [79, current.VERSION])
def test_full_contract_does_not_hide_a_trigger_sharing_a_required_table_name(installed_inputs, version):
    with closing(sqlite3.connect(':memory:')) as conn:
        installed_inputs(version).backup(conn)
        runner.validate_version(conn, version)
        conn.execute('CREATE TRIGGER settings AFTER INSERT ON settings BEGIN SELECT 1; END')
        with pytest.raises(runner.MigrationError, match='Missing current table: settings'):
            runner.validate_version(conn, version)
        conn.execute('BEGIN')
        with pytest.raises(runner.MigrationError, match='Missing current table: settings'):
            runner.validate_version(conn, version)
        conn.rollback()
        conn.execute('DROP TRIGGER settings')
        runner.validate_version(conn, version)


def validate_installed_owner(module, conn):
    if module is v061_recording_vibes:
        module.validate(conn, replaced={'audio_vibe_feedback', 'vibe_artists_insert',
                                        'vibe_artists_update', 'vibe_artists_delete'})
    else:
        module.validate(conn)


@pytest.mark.parametrize('module', VALIDATORS, ids=lambda module: module.__name__.rsplit('.', 1)[1])
def test_owner_validation_rechecks_missing_changed_and_restored_definitions(installed_inputs, module):
    with closing(sqlite3.connect(':memory:')) as conn:
        installed_inputs(79).backup(conn)
        validate_installed_owner(module, conn)
        sql = next(sql for sql in module.statements()
                   if sql.startswith(('CREATE INDEX ', 'CREATE UNIQUE INDEX ')))
        name = sql.split()[3 if sql.startswith('CREATE UNIQUE') else 2]
        table, original = conn.execute(
            "SELECT tbl_name,sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)).fetchone()
        quote = lambda value: '"' + value.replace('"', '""') + '"'
        conn.execute('DROP INDEX ' + quote(name))
        with pytest.raises(runner.MigrationError):
            validate_installed_owner(module, conn)
        column = conn.execute('PRAGMA table_info(' + quote(table) + ')').fetchone()[1]
        conn.execute('CREATE INDEX ' + quote(name) + ' ON ' + quote(table) +
                     '(' + quote(column) + ') WHERE 0')
        with pytest.raises(runner.MigrationError):
            validate_installed_owner(module, conn)
        conn.execute('DROP INDEX ' + quote(name))
        conn.execute(original)
        validate_installed_owner(module, conn)


@pytest.mark.parametrize('module,retired', [
    (v061_recording_vibes, ('audio_vibe_samples', 'rec_vibe_tracks', 'rec_vibe_groups')),
    (v067_audio_annotations, ('audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels')),
    (v068_canonical_embeddings, tuple(v068_canonical_embeddings.SOURCES)),
    (v070_policy_documents, tuple(sorted(v070_policy_documents.REPLACED))),
    (v072_compact_groups, v072_compact_groups.OLD_TABLES),
    (v073_recommendation_publications,
     ('rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds')),
], ids=lambda value: value.__name__.rsplit('.', 1)[1] if hasattr(value, '__name__') else None)
def test_owner_validation_rejects_every_superseded_table(installed_inputs, module, retired):
    with closing(sqlite3.connect(':memory:')) as conn:
        installed_inputs(79).backup(conn)
        validate_installed_owner(module, conn)
        for name in retired:
            conn.execute('CREATE TABLE "' + name + '" (obsolete INTEGER)')
            with pytest.raises(runner.MigrationError, match='Superseded'):
                validate_installed_owner(module, conn)
            conn.execute('DROP TABLE "' + name + '"')
            validate_installed_owner(module, conn)


@pytest.mark.parametrize('module', (embedding_files, group_retention_progress, mix_choices, profile_files),
                         ids=lambda module: module.__name__.rsplit('.', 1)[1])
def test_regular_owner_validation_rechecks_missing_changed_and_restored_definitions(installed_inputs, module):
    with closing(sqlite3.connect(':memory:')) as conn:
        installed_inputs(79, completed_regular=True).backup(conn)
        module.validate(conn)
        definitions = (module.definitions(conn) if module is group_retention_progress else module.definitions())
        name, _ = next((name, sql) for name, sql in definitions
                       if sql.startswith('CREATE TRIGGER '))
        table, original = conn.execute(
            "SELECT tbl_name,sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)).fetchone()
        conn.execute('DROP TRIGGER "' + name + '"')
        with pytest.raises((runner.MigrationError, ValueError)):
            module.validate(conn)
        conn.execute('CREATE TRIGGER "' + name + '" AFTER INSERT ON "' + table + '" BEGIN SELECT 1; END')
        with pytest.raises((runner.MigrationError, ValueError)):
            module.validate(conn)
        conn.execute('DROP TRIGGER "' + name + '"')
        conn.execute(original)
        module.validate(conn)


@pytest.mark.parametrize('validate,name', [
    (credit_retention.validate, 'immutable_artist_credit_replay'),
    (mode_retention.validate, 'monotonic_taste_mode_allocation'),
    (served_facts.validate, 'served_fact_immutable'),
    (metadata_runtime.validate, 'ix_metadata_runtime_conflict_recording'),
    (group_progress.validate, 'group_progress_legacy_insert'),
    (group_checkpoint_retention.validate, 'group_retention_checkpoint_insert'),
    (vibe_provenance.validate_allocation, 'prediction_id_monotonic'),
    (vibe_provenance.validate_guards, 'prediction_provenance_expiry'),
    (vibe_provenance.validate_lifecycle, 'prediction_inputs_file_insert'),
], ids=lambda value: value.__module__.rsplit('.', 1)[1] + '.' + value.__name__ if callable(value) else value)
def test_batched_regular_validators_observe_live_missing_changed_and_restored_objects(
        installed_inputs, validate, name):
    with closing(sqlite3.connect(':memory:')) as conn:
        installed_inputs(94, completed_regular=True).backup(conn)
        validate(conn)
        kind, table, original = conn.execute(
            'SELECT type,tbl_name,sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        quoted_name = '"' + name + '"'
        quoted_table = '"' + table + '"'
        conn.execute('DROP ' + kind + ' ' + quoted_name)
        with pytest.raises((runner.MigrationError, ValueError)):
            validate(conn)
        if kind == 'trigger':
            conn.execute('CREATE TRIGGER ' + quoted_name + ' AFTER INSERT ON ' + quoted_table +
                         ' BEGIN SELECT 1; END')
        else:
            column = conn.execute('PRAGMA table_info(' + quoted_table + ')').fetchone()[1]
            conn.execute('CREATE INDEX ' + quoted_name + ' ON ' + quoted_table +
                         '("' + column + '") WHERE 0')
        with pytest.raises((runner.MigrationError, ValueError)):
            validate(conn)
        conn.execute('DROP ' + kind + ' ' + quoted_name)
        conn.execute(original)
        validate(conn)


@pytest.mark.parametrize('version', [85, 93, 103, 106])
def test_repeated_validation_rejects_later_damage_and_separate_damaged_database(
        installed_inputs, version):
    with ExitStack() as resources:
        connections = []
        for _ in range(2):
            conn = resources.enter_context(closing(sqlite3.connect(':memory:')))
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            installed_inputs(version).backup(conn)
            connections.append(conn)

        runner.validate_version(connections[0], version)
        for conn in connections:
            original = list(conn.iterdump())
            conn.execute('DROP INDEX ix_entries_recording')
            for full_audit in (False, True):
                with pytest.raises(runner.MigrationError, match='ix_entries_recording'):
                    runner.validate_version(conn, version, foreign_keys=full_audit, data=full_audit)
            conn.execute('CREATE INDEX ix_entries_recording ON catalog_entries(video_id)')
            with pytest.raises(runner.MigrationError, match='ix_entries_recording'):
                runner.validate_version(conn, version)
            conn.execute('DROP INDEX ix_entries_recording')
            conn.execute('CREATE INDEX ix_entries_recording ON catalog_entries(recording_id)')
            restored = list(conn.iterdump())
            runner.validate_version(conn, version)
            assert list(conn.iterdump()) == restored
            assert sorted(restored) == sorted(original)


@pytest.mark.parametrize('version', [79, current.VERSION])
def test_completed_regular_history_is_admitted_once_without_writing(
        installed_inputs, monkeypatch, version):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        installed_inputs(version, completed_regular=True).backup(conn)
        before = list(conn.iterdump())
        changes = conn.total_changes
        conn.execute('PRAGMA query_only=ON')
        observed = []
        validate = runner.validate_version

        def record(*args, **kwargs):
            observed.append((args[1], kwargs))
            return validate(*args, **kwargs)

        def unnecessary(*args, **kwargs):
            pytest.fail('Completed regular history restarted the migration runner')

        monkeypatch.setattr(runner, 'validate_version', record)
        monkeypatch.setattr(regular, 'migrate', unnecessary)
        assert runner.migrate(conn) == (version < current.VERSION)
        assert observed == [(version, {'foreign_keys': False, 'data': False})]
        assert not conn.in_transaction
        assert conn.total_changes == changes
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize('version', [79, current.VERSION])
@pytest.mark.parametrize('damage', ['installed_owner', 'regular_owner', 'regular_history'])
def test_completed_regular_admission_rechecks_damage(
        installed_inputs, version, damage):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        installed_inputs(version, completed_regular=True).backup(conn)
        runner.migrate(conn)
        with conn:
            if damage == 'installed_owner':
                conn.execute('DROP INDEX ix_entries_recording')
            elif damage == 'regular_owner':
                conn.execute('DROP TABLE playback_feedback_receipts')
            else:
                conn.execute("UPDATE schema_changes SET name='damaged' WHERE version=1")
        before = list(conn.iterdump())
        with pytest.raises(runner.MigrationError):
            runner.migrate(conn)
        assert list(conn.iterdump()) == before
