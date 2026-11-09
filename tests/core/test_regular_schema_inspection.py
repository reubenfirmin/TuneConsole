"""Reuse structural observations without accepting stale migration inputs."""
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3
import time

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core.migrations import MigrationError, regular, registry, runner
from yt_playlist.core import genre_identity, schema_definition


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL-only runner checks never start the web lifespan or sync worker."""


@pytest.fixture(scope='module')
def regular_layout():
    """Build the exact empty installed input, without replaying empty upgrades."""
    with pytest.MonkeyPatch.context() as patch, closing(sqlite3.connect(':memory:')) as source:
        bound_catalog(patch)
        source.execute('PRAGMA foreign_keys=ON')
        source.execute('BEGIN')
        for sql in schema_definition.statements(version=93):
            source.execute(sql)
        source.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
            [(item.version, item.name) for item in registry.MIGRATIONS[:93]])
        source.executemany('INSERT INTO settings VALUES (?,?)', schema_definition.SEED_SETTINGS.items())
        for sql in schema_definition.INITIAL_STATE:
            source.execute(sql)
        genre_identity.seed_catalog(source)
        source.commit()
        source.execute('PRAGMA query_only=ON')
        yield source


class ReservationConnection(sqlite3.Connection):
    before_reservation = None

    def execute(self, sql, *args, **kwargs):
        if sql == 'BEGIN IMMEDIATE' and self.before_reservation is not None:
            callback, self.before_reservation = self.before_reservation, None
            assert not self.in_transaction
            callback()
        return super().execute(sql, *args, **kwargs)


@pytest.fixture
def installed(tmp_path, regular_layout):
    path = tmp_path / 'installed.db'
    with closing(sqlite3.connect(path, factory=ReservationConnection)) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        regular_layout.backup(conn)
        yield path, conn


@pytest.fixture(scope='module')
def regular_prefixes(regular_layout):
    """Freeze genuine successful prefixes once; failure cases own writable copies."""
    from tests.prepared_fixture_support import coordinates

    with ExitStack() as resources:
        builder = resources.enter_context(closing(sqlite3.connect(':memory:')))
        regular_layout.backup(builder)
        history = list(builder.execute('SELECT * FROM schema_migrations ORDER BY version'))
        sources, fingerprints = {}, {}

        def predecessor(completed):
            assert 0 <= completed < len(regular.CHANGES)
            while len(sources) <= completed:
                version = len(sources)
                if version:
                    regular.migrate(builder, changes=regular.CHANGES[:version])
                assert applied(builder) == [(change.version, change.name)
                                            for change in regular.CHANGES[:version]]
                assert list(builder.execute('SELECT * FROM schema_migrations ORDER BY version')) == history
                assert not coordinates(builder), 'SQL-only regular prefix retains external files'
                source = resources.enter_context(closing(sqlite3.connect(':memory:')))
                builder.backup(source)
                source.execute('PRAGMA query_only=ON')
                sources[version] = source
                fingerprints[version] = sha256(source.serialize()).digest()
            return sources[completed]

        yield predecessor
        for version, source in sources.items():
            assert sha256(source.serialize()).digest() == fingerprints[version]


def applied(conn):
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_changes'").fetchone():
        return []
    return list(conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))


@pytest.fixture(scope='module')
def completed_regular(regular_layout):
    with closing(sqlite3.connect(':memory:')) as source:
        regular_layout.backup(source)
        regular.migrate(source)
        source.execute('PRAGMA query_only=ON')
        yield source


@pytest.fixture
def completed_installed(tmp_path, completed_regular):
    path = tmp_path / 'completed-regular.db'
    with closing(sqlite3.connect(path, factory=ReservationConnection)) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        completed_regular.backup(conn)
        yield path, conn


def test_completed_regular_input_keeps_real_history_and_read_only_sql(completed_regular):
    assert applied(completed_regular) == [(change.version, change.name) for change in regular.CHANGES]
    assert completed_regular.execute('SELECT max(version) FROM schema_migrations').fetchone() == (93,)
    assert not completed_regular.execute('PRAGMA foreign_key_check').fetchone()
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        completed_regular.execute("INSERT INTO settings VALUES ('private','forbidden')")


def test_regular_prefix_input_keeps_exact_committed_history_and_read_only_sql(regular_prefixes):
    source = regular_prefixes(1)
    assert applied(source) == [(regular.CHANGES[0].version, regular.CHANGES[0].name)]
    assert source.execute('PRAGMA query_only').fetchone() == (1,)
    assert not source.execute('PRAGMA foreign_key_check').fetchone()


def contents(conn):
    # The runner creates its empty ledger in the first change's transaction.
    # Its rollback can remove that empty table; every other schema/data byte
    # represented by iterdump must match the state before the failed change.
    return [line for line in conn.iterdump()
            if not line.startswith('CREATE TABLE schema_changes ')]


def test_regular_session_inspects_each_unchanged_table_once(
        tmp_path, regular_layout, monkeypatch):
    with closing(sqlite3.connect(tmp_path / 'installed.db')) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        regular_layout.backup(conn)
        statements, validations = [], []
        original = regular.validate_version

        def validate(*args, **kwargs):
            validations.append(args[1])
            return original(*args, **kwargs)

        monkeypatch.setattr(regular, 'validate_version', validate)
        conn.set_trace_callback(statements.append)
        started = time.perf_counter()
        regular.migrate(conn)
        elapsed = time.perf_counter() - started
        inspections = Counter(sql for sql in statements
                              if sql.upper().startswith('PRAGMA MAIN.TABLE_XINFO("'))
        print(json.dumps({'seconds': elapsed, 'validations': len(validations),
                          'sql_statements': len(statements),
                          'table_inspections': sum(inspections.values()),
                          'distinct_table_inspections': len(inspections),
                          'max_repeated_inspection': max(inspections.values())}))
        # Initial read plan, every reserved change, and final read admission.
        assert len(validations) == len(regular.CHANGES) + 2
        assert max(inspections.values()) == 1


@pytest.mark.parametrize('mutation,error', [
    ("UPDATE schema_migrations SET name='changed' WHERE version=93", 'contiguous prefix'),
    ('ALTER TABLE identities RENAME COLUMN label TO damaged', 'identities'),
    ("UPDATE settings SET value='changed' WHERE key='catalog_identity_version'", 'catalog identity'),
])
def test_concurrent_damage_between_plan_and_reservation_is_rejected(installed, mutation, error):
    path, conn = installed

    def change():
        with closing(sqlite3.connect(path)) as other, other:
            other.execute(mutation)

    conn.before_reservation = change
    with pytest.raises(MigrationError, match=error):
        regular.migrate(conn)
    assert not applied(conn)
    assert not conn.in_transaction


@pytest.mark.parametrize('advance', ['regular', 'historical'])
def test_concurrent_advancement_is_read_under_reservation(installed, advance, monkeypatch):
    path, conn = installed
    observed, validate = [], regular.validate_version

    def record(connection, version, **kwargs):
        if connection is conn:
            observed.append(version)
        return validate(connection, version, **kwargs)

    monkeypatch.setattr(regular, 'validate_version', record)

    def change():
        with closing(sqlite3.connect(path)) as other:
            other.row_factory = sqlite3.Row
            if advance == 'regular':
                regular.migrate(other, changes=regular.CHANGES[:1])
            else:
                runner.migrate(other, registry=registry.MIGRATIONS[:94])

    conn.before_reservation = change
    regular.migrate(conn)
    assert applied(conn) == [(change.version, change.name) for change in regular.CHANGES]
    expected = 94 if advance == 'historical' else 93
    assert observed[:2] == [93, expected]
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone() == (expected,)


def test_external_row_commit_renews_structural_observations(installed):
    path, conn = installed
    statements = []
    conn.set_trace_callback(statements.append)

    def change():
        with closing(sqlite3.connect(path)) as other, other:
            other.execute("INSERT INTO settings VALUES ('concurrent-setting','committed')")

    conn.before_reservation = change
    regular.migrate(conn)
    assert statements.count('PRAGMA main.table_xinfo("identities")') == 2


def test_later_incompatible_regular_owner_is_rejected(installed):
    path, conn = installed
    first = regular.CHANGES[0]

    def damage():
        with closing(sqlite3.connect(path)) as other, other:
            other.execute('DROP TABLE playback_feedback_receipts')
            other.execute('CREATE TABLE playback_feedback_receipts(capture_id TEXT)')

    def install(connection, context):
        first.apply(connection, context)
        # The first change commits before the next independent reservation.
        conn.before_reservation = damage

    with pytest.raises(MigrationError, match='playback_feedback_receipts'):
        regular.migrate(conn, changes=(replace(first, apply=install), *regular.CHANGES[1:]))
    assert applied(conn) == [(first.version, first.name)]
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone() == (93,)


def interrupted_change(conn, failed, failure, changes, *, predecessor=None):
    """Fail a real runner turn and compare its exact committed predecessor."""
    case = f'{failed.version} {failed.name} ({failure})'
    interruption = f'Interrupted bounded change {failed.version} during {failure}: {failed.name}'
    historical = list(conn.execute('SELECT * FROM schema_migrations ORDER BY version'))
    before = []
    committed = []

    def apply(connection, context):
        if predecessor is None:
            before.extend(contents(connection))
        committed.extend(connection.execute('SELECT * FROM schema_changes ORDER BY version'))
        failed.apply(connection, context)
        if failure == 'apply':
            raise RuntimeError(interruption)

    def validate(connection):
        failed.validate(connection)
        if failure == 'validate':
            raise RuntimeError(interruption)

    changes = tuple(replace(change, apply=apply, validate=validate)
                    if change is failed else change for change in changes)
    with pytest.raises(RuntimeError, match=f'Interrupted bounded change {failed.version} during {failure}:'):
        regular.migrate(conn, changes=changes)
    assert not conn.in_transaction, case
    assert applied(conn) == [(change.version, change.name)
                             for change in regular.CHANGES[:failed.version - 1]], case
    if predecessor is None:
        assert contents(conn) == before, case
    else:
        assert conn.serialize() == predecessor, case
    assert list(conn.execute('SELECT * FROM schema_migrations ORDER BY version')) == historical, case
    if committed:
        assert list(conn.execute('SELECT * FROM schema_changes ORDER BY version')) == committed, case
    return committed


@pytest.mark.parametrize('failed', regular.CHANGES[:-1], ids=lambda change: f'{change.version}-{change.name}')
def test_each_change_rolls_back_and_can_retry(tmp_path, regular_prefixes, failed, monkeypatch):
    with closing(sqlite3.connect(tmp_path / 'retry.db')) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        regular_prefixes(failed.version - 1).backup(conn)
        # This unit owns the selected change's SQL, validation and rollback.
        # Its genuine prefix is built by the unmodified runner above. Historical
        # layout admission and concurrent damage retain real validators in
        # test_regular_session_inspects_each_unchanged_table_once and the
        # concurrent/later-incompatible-owner cases in this module.
        monkeypatch.setattr(regular, 'validate_version', lambda *args, **kwargs: None)
        check_change_retry(conn, failed)


def check_change_retry(conn, failed):
    historical = list(conn.execute('SELECT * FROM schema_migrations ORDER BY version'))
    prefix = regular.CHANGES[:failed.version]
    # Each independently diagnosed change keeps both genuine fault boundaries,
    # its exact committed predecessor, successful retry and writable restart.
    for failure in ('apply', 'validate'):
        # Only this change is pending. Compare the complete SQLite image before
        # and after rollback, rather than re-encoding every empty table and DDL
        # statement four times. Multi-change runs below retain their committed
        # prefixes and still compare logical contents at the failed boundary.
        predecessor = conn.serialize()
        committed = interrupted_change(conn, failed, failure, prefix, predecessor=predecessor)
    regular.migrate(conn, changes=prefix)
    assert applied(conn) == [(change.version, change.name) for change in prefix], failed.name
    assert list(conn.execute('SELECT * FROM schema_changes WHERE version<? ORDER BY version',
                             (failed.version,))) == committed, failed.name
    assert list(conn.execute('SELECT * FROM schema_migrations ORDER BY version')) == historical
    assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    with conn:
        conn.execute("INSERT INTO settings VALUES ('after-prefix-retry','writable')")
    regular.migrate(conn, changes=prefix)
    assert conn.execute("SELECT value FROM settings WHERE key='after-prefix-retry'").fetchone() == ('writable',)


@pytest.mark.parametrize('failure', ['apply', 'validate'])
def test_final_change_rolls_back_and_can_retry_from_original_layout(installed, failure):
    _, conn = installed
    failed = regular.CHANGES[-1]
    historical = list(conn.execute('SELECT * FROM schema_migrations ORDER BY version'))
    committed = interrupted_change(conn, failed, failure, regular.CHANGES)
    regular.migrate(conn)
    assert applied(conn) == [(change.version, change.name) for change in regular.CHANGES]
    assert list(conn.execute('SELECT * FROM schema_changes WHERE version<? ORDER BY version',
                             (failed.version,))) == committed
    assert list(conn.execute('SELECT * FROM schema_migrations ORDER BY version')) == historical
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone() == (93,)


def test_later_calls_and_connections_reinspect_live_tables(completed_installed):
    path, conn = completed_installed
    statements = []
    conn.set_trace_callback(statements.append)
    regular.migrate(conn)
    assert statements.count('PRAGMA main.table_xinfo("identities")') == 1
    with closing(sqlite3.connect(path)) as later:
        statements.clear()
        later.set_trace_callback(statements.append)
        regular.migrate(later)
        assert statements.count('PRAGMA main.table_xinfo("identities")') == 1
        with later:
            later.execute('ALTER TABLE identities RENAME COLUMN label TO damaged')
    with pytest.raises(MigrationError, match='identities'):
        regular.migrate(conn)


def test_private_regular_prefix_upgrade_keeps_other_connections_and_source_unchanged(installed, regular_prefixes):
    path, conn = installed
    source = regular_prefixes(1)
    with closing(sqlite3.connect(path.with_name('private-prefix.db'))) as private:
        source.backup(private)
        with private:
            private.execute("INSERT INTO settings VALUES ('private-prefix-setting','changed')")
        regular.migrate(private)
        assert applied(private) == [(change.version, change.name) for change in regular.CHANGES]
        assert applied(source) == [(regular.CHANGES[0].version, regular.CHANGES[0].name)]
        for other in (source, conn):
            assert other.execute("SELECT 1 FROM settings WHERE key='private-prefix-setting'").fetchone() is None
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            with source:
                source.execute("INSERT INTO settings VALUES ('private-prefix-setting','forbidden')")


@pytest.mark.parametrize('malformed', [False, True], ids=['incomplete', 'malformed'])
def test_pending_regular_ddl_keeps_preparation_incomplete(installed, malformed):
    from yt_playlist.core.preparation import model_checkpoints

    _, conn = installed
    conn.row_factory = sqlite3.Row
    raw = 'NaN' if malformed else '123'
    with conn:
        conn.execute("INSERT INTO settings VALUES ('rec_candidates_expire_at',?)", (raw,))
    job = model_checkpoints.JOBS['candidate-checkpoints']
    job.install(conn)
    if malformed:
        with pytest.raises(ValueError):
            job.step(conn)
    before = job.progress(conn)
    assert before.status != 'prepared'
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    assert not applied(conn)
    regular.migrate(conn)
    assert len(applied(conn)) == len(regular.CHANGES)
    assert job.progress(conn) == before
    assert conn.execute("SELECT value FROM settings WHERE key='rec_candidates_expire_at'").fetchone()[0] == raw
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
