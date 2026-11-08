"""Repeated admission must inspect each installed database's current structure."""
from contextlib import ExitStack, closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, install_regular_input
from yt_playlist.core.migrations import current, regular, runner


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
