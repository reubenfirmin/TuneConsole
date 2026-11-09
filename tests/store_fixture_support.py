"""Bind ordinary consumers to private copies of the shipped empty schema.

Admission, bootstrap, upgrade and repair tests construct or reopen real Stores.
Consumer fixtures reuse immutable SQL built below startup admission;
they retain real repositories, revision guards, repair and WAL configuration.
"""
from copy import deepcopy
from contextlib import closing, contextmanager, nullcontext
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import repair
from yt_playlist.core.db_revisions import Revisions


@pytest.fixture
def without_query_timing(monkeypatch):
    """Keep real SQL, handle admission and locking below ordinary consumers.

    Query timing and slow-query reporting have their own diagnostic tests.
    """
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.core.db_handles import Connection

    monkeypatch.setattr(DiagnosticConnection, 'cursor', Connection.cursor)


@pytest.fixture
def native_sql_calls(monkeypatch):
    """Real SQLite beneath synchronous consumers of admitted, uncontended SQL.

    Handle admission and thread coordination retain their instrumented owners.
    These consumers still execute actual constraints, transactions and triggers.
    """
    use_native_sql_calls(monkeypatch)


def use_native_sql_calls(monkeypatch):
    """Supply native SQL below higher-scope fixture producers as well."""
    from yt_playlist.core.db_diagnostics import DiagnosticConnection

    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))


@pytest.fixture
def file_consumer_store(tmp_path, _pristine_current_schema, monkeypatch):
    """Private WAL input for consumers that do not own snapshot construction."""
    with _file_consumer_store(tmp_path, _pristine_current_schema) as database:
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture
def uncontended_file_consumer_store(tmp_path, _pristine_current_schema, monkeypatch):
    """Real private SQL/files below consumers of successful snapshot coordination."""
    with _file_consumer_store(tmp_path, _pristine_current_schema, snapshot_guards=False) as database:
        with mock_snapshot_coordinator(database, monkeypatch):
            yield database


@contextmanager
def _file_consumer_store(tmp_path, pristine, *, snapshot_guards=True):
    from yt_playlist.core import schema_inspection
    from yt_playlist.core.store import Store

    directory = tmp_path / 'consumer'
    directory.mkdir()
    database = Store(directory / 'consumer.db')
    with closing(database.conn):
        # Consumers own SQL/files and transaction visibility. Their disposable
        # databases need no power-loss durability at copy, commit or cleanup.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, pristine, snapshot_guards=snapshot_guards)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with schema_inspection.reuse_tables(database.conn):
            yield database


@pytest.fixture
def publication_schema_inspections(request):
    """Cache unchanged table observations within one publication case.

    Validators still check live SQL and refresh after external commits. Keep
    reader and artifact leases entirely under each test's control.
    """
    fixture = next((name for name in ('installed', 'unadmitted_installed', 'guarded_installed', 'numbered_store')
                    if name in request.fixturenames), None)
    if fixture is None:
        yield
        return
    from yt_playlist.core import schema_inspection

    store, _ = request.getfixturevalue(fixture)
    with schema_inspection.reuse_tables(store.conn):
        yield


@pytest.fixture
def publication_preparation_input(request, monkeypatch):
    """Use the controlled installed input below publication's preparation work.

    Admission owners and explicit Store startup retain real base validation.
    Publication keeps its preparation tables, SQL, files and custody checks.
    """
    fixture = next((name for name in ('installed', 'unadmitted_installed', 'guarded_installed', 'numbered_store')
                    if name in request.fixturenames), None)
    if fixture is None:
        return
    store, _ = request.getfixturevalue(fixture)
    _publication_preparation_input(store, monkeypatch)


def _publication_preparation_input(store, monkeypatch):
    if (hasattr(store, '_fixture_installed_template')
            or hasattr(store, '_fixture_numbered_input_version')):
        from yt_playlist.core.preparation import listening_subjects, rows

        monkeypatch.setattr(rows, 'validate_version', lambda *args, **kwargs: None)
        monkeypatch.setattr(listening_subjects, 'validate_version', lambda *args, **kwargs: None)


def mock_preparation_schema(monkeypatch, job, conn):
    """Admit installed SQL once below tests of data/file preparation.

    Use only when the test keeps this job's SQL definitions unchanged. Worker
    progress, data verification, source fences and publication remain real.
    """
    job.validate(conn)
    validate = type(job).validate

    def admitted(worker, target):
        if worker is not job or target is not conn:
            return validate(worker, target)

    monkeypatch.setattr(type(job), 'validate', admitted)


@pytest.fixture
def publication_consumer_reads(request, monkeypatch):
    """Reuse real reporting reads below installed publication workflows.

    Explicit snapshots, reopens, publication guards and final installed-format
    validation keep their own connections. Independent native-upgrade cases
    in the same module have no installed consumer to configure.
    """
    fixture = next((name for name in ('installed', 'unadmitted_installed', 'guarded_installed')
                    if name in request.fixturenames), None)
    if fixture is None:
        yield
        return
    from yt_playlist.core import schema_inspection

    store, _ = request.getfixturevalue(fixture)
    with monkeypatch.context() as patch, reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
        _publication_preparation_input(store, patch)
        yield


def assert_installed_consumer_input(store, version):
    """Check completed numbered SQL and private guards beneath consumers."""
    from yt_playlist.core.migrations import registry, regular

    source = store._fixture_installed_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, store.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:version]]
    # Admission belongs to its independent owners. Check the private copy's
    # actual completed regular history, including an unfinished regular prefix.
    def history(conn):
        if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='schema_changes'").fetchone():
            return []
        return list(map(tuple, conn.execute('SELECT version,name FROM schema_changes ORDER BY version')))

    expected = history(source.conn)
    assert history(store.conn) == expected
    assert regular.validate(store.conn) == len(expected)
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def revision_definitions(conn):
    """Read the complete connection-local snapshot guard definitions."""
    return tuple(conn.execute(
        "SELECT type,name,sql FROM sqlite_temp_schema WHERE "
        "(type='trigger' AND name LIKE 'snapshot_revision_%') OR "
        "(type='table' AND name='snapshot_revisions') ORDER BY type,name").fetchall())


class PristineSchema(sqlite3.Connection):
    """Keep genuinely generated connection-local guards beside the SQL seed."""

    def capture_revisions(self, revisions):
        assert not revisions.conn.execute('SELECT 1 FROM temp.snapshot_revisions').fetchone()
        statements = [sql.replace('CREATE ', 'CREATE TEMP ', 1)
                      for _, _, sql in revision_definitions(revisions.conn)]
        self._revision_script = 'BEGIN;\n' + ';\n'.join(statements) + ';\nCOMMIT;'
        self._revision_state = deepcopy({name: value for name, value in vars(revisions).items()
                                         if name != 'conn'})

    def bind_revisions(self, conn):
        # SQLite backup excludes TEMP. Install the original generated SQL in a
        # new private TEMP namespace, avoiding its repeated Python construction.
        assert not conn.in_transaction
        conn.executescript(self._revision_script)
        revisions = Revisions.__new__(Revisions)
        vars(revisions).update(deepcopy(self._revision_state))
        revisions.conn = conn
        revisions.schema_version = conn.execute('PRAGMA main.schema_version').fetchone()[0]
        return revisions


@contextmanager
def consumer_schema(*, entries=None, snapshot_guards=True):
    """Shipped SQL for consumers below admission, with optional genre bounds.

    This deliberately does not validate schema admission. Upgrade and startup
    owners must exercise their real validators separately.
    """
    import pytest
    from tests.genre_catalog_support import bound_catalog
    from yt_playlist.core import destination_schema, genre_identity, schema_bootstrap
    from yt_playlist.core.migrations import registry, regular

    with closing(sqlite3.connect(':memory:', factory=PristineSchema)) as schema:
        schema.execute('PRAGMA foreign_keys=ON')
        document = destination_schema.definition()
        with pytest.MonkeyPatch.context() as patch, schema:
            if entries is not None:
                seeds = [sql for sql in document['destination_seeds'] if not sql.startswith(
                    ('INSERT INTO "genre_alias"', 'INSERT INTO "genre_catalog"', 'INSERT INTO "genre_family"'))]
                bound_catalog(patch, entries=entries)
                patch.setattr(destination_schema, 'definition', lambda: {**document, 'destination_seeds': seeds})
            schema.execute('BEGIN')
            schema_bootstrap.bootstrap(schema, registry.MIGRATIONS, regular.CHANGES,
                                       1., validation=lambda conn: None)
            if entries is not None:
                genre_identity.seed_catalog(schema)
        if snapshot_guards:
            # Generate the real TEMP guards in one transaction instead of an
            # implicit transaction for every trigger. Revisions commits it.
            schema.execute('BEGIN')
            schema.capture_revisions(Revisions(schema))
        schema.execute('PRAGMA query_only=ON')
        yield schema


def bounded_consumer_schema(entries, *, snapshot_guards=True):
    return consumer_schema(entries=entries, snapshot_guards=snapshot_guards)


def initialize_consumer(store, pristine, *, snapshot_guards=True, compatibility=False):
    assert pristine.execute('PRAGMA query_only').fetchone()[0] == 1
    pristine.backup(store.conn)
    store._schema_compatibility = compatibility
    store._rebuild_source_version = None
    store._schema_recovery_pending = False
    store._bind_repositories()
    if not compatibility:
        repair.run_once(store)
    if store._db_path is not None:
        mode = store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        assert mode.lower() == 'wal'
    if snapshot_guards:
        store._snapshot_revisions = pristine.bind_revisions(store.conn)


@pytest.fixture
def numbered_store(request, tmp_path, monkeypatch):
    """Build shipped predecessor SQL without running application startup.

    Registered upgrades still admit and convert their actual populated input.
    Consumers needing snapshots use guarded_numbered_store instead.
    """
    with _numbered_input(request.param, tmp_path, monkeypatch) as value:
        yield value


@pytest.fixture
def guarded_numbered_store(request, tmp_path, monkeypatch):
    """Private predecessor SQL with real revision guards and WAL snapshots."""
    with _numbered_input(request.param, tmp_path, monkeypatch, snapshot_guards=True) as value:
        yield value


@pytest.fixture
def preparation_ddl_connection(request, monkeypatch):
    """A real SQLite DDL worker beside the preparation's live Store.

    These tests own migration progress alongside incomplete preparation. SQL
    diagnostics and numbered-layout admission have independent owners. Keep
    every regular change, its validation, ledger and real transaction live.
    """
    fixture = next(name for name in ('installed', 'unadmitted_installed', 'guarded_installed', 'numbered_store')
                   if name in request.fixturenames)
    store, _ = request.getfixturevalue(fixture)
    with preparation_connection(store) as conn:
        from yt_playlist.core.migrations import regular

        validate = regular.validate_version

        def admitted(target, *args, **kwargs):
            if target is not conn:
                return validate(target, *args, **kwargs)

        # The DDL worker consumes the installed fixture's completed numbered
        # layout. Other connections, including explicit startup/upgrade checks,
        # still perform actual admission. The runner still checks both live
        # histories and validates every regular owner before and after DDL.
        monkeypatch.setattr(regular, 'validate_version', admitted)
        yield conn


@contextmanager
def preparation_connection(store):
    """Own a real SQLite worker on a Store's private database file."""
    from yt_playlist.core import schema_inspection

    with closing(sqlite3.connect(store._db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        # Keep actual WAL commits, visibility and rollback. Machine power loss
        # is outside the contract of these disposable preparation workers.
        conn.execute('PRAGMA synchronous=OFF')
        with schema_inspection.reuse_tables(conn):
            yield conn


@contextmanager
def _numbered_input(version, tmp_path, monkeypatch, *, snapshot_guards=False):
    from tests.genre_catalog_support import bound_catalog
    from yt_playlist.core import schema_inspection
    from yt_playlist.core.store import Store

    bound_catalog(monkeypatch)
    database = Store(tmp_path / 'numbered-input.db')
    with closing(database.conn):
        # Preparation keeps real SQL transactions and files. Power-loss
        # durability of this disposable input is outside those tests.
        database.conn.execute('PRAGMA synchronous=OFF')
        with closing(sqlite3.connect(':memory:')) as schema:
            install_numbered_input(schema, version)
            schema.backup(database.conn)
        database._fixture_numbered_input_version = version
        database.conn.execute('PRAGMA journal_mode=WAL')
        database._schema_compatibility = version < 103
        database._rebuild_source_version = None
        database._schema_recovery_pending = False
        database._bind_repositories()
        if snapshot_guards:
            database._snapshot_revisions = Revisions(database.conn)
        with schema_inspection.reuse_tables(database.conn):
            yield database, version



def install_numbered_input(conn, version, *, regular_owners=()):
    """Completed numbered SQL below an upgrade or publication test boundary.

    Real migration runners still own the edge under test. This supplies their
    exact empty predecessor contract without replaying every earlier conversion.
    Callers choose their genre vocabulary and any completed regular owners.
    """
    from yt_playlist.core import genre_identity, schema_admission, schema_definition
    from yt_playlist.core.migrations import registry
    from yt_playlist.core.migrations.runner import Context

    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    with conn:
        conn.execute('BEGIN')
        # The assembled v86 contract deliberately admits optional v80-v85
        # owners. A publication predecessor needs their actual absence before
        # takeover, so start at v79 and apply only those small historical edges.
        base_version = 79 if 79 <= version < 86 else version
        statements = (schema_admission.reference()['statements'] if base_version == 79
                      else schema_definition.statements(version=version))
        for sql in statements:
            conn.execute(sql)
        conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                         [(item.version, item.name) for item in registry.MIGRATIONS[:base_version]])
        conn.executemany('INSERT INTO settings VALUES (?,?)', schema_definition.SEED_SETTINGS.items())
        for sql in schema_definition.INITIAL_STATE:
            conn.execute(sql)
        genre_identity.seed_catalog(conn)
        for migration in registry.MIGRATIONS[base_version:version]:
            with schema_definition.migration_definitions(migration.version):
                migration.apply(conn, Context(1.))
            conn.execute('INSERT INTO schema_migrations VALUES (?,?,1)',
                         (migration.version, migration.name))
        if regular_owners:
            install_regular_input(conn, regular_owners)


def install_regular_input(conn, changes):
    """Supply completed lower regular owners before the real edge under test.

    The caller owns the transaction. The runner being tested still validates
    this predecessor and owns the next change's commit, rollback and ledger.
    """
    from yt_playlist.core.migrations import regular
    from yt_playlist.core.migrations.runner import Context

    conn.execute(regular.HISTORY_SQL)
    for change in changes:
        change.apply(conn, Context(1.))
        conn.execute('INSERT INTO schema_changes VALUES (?,?,1)', (change.version, change.name))


@contextmanager
def mock_snapshot_coordinator(store, monkeypatch):
    """Real private SQL reads/writes below consumers of snapshot coordination.

    Conflict detection and source-change races use actual guarded snapshots in
    their own tests. This helper supplies uncontended publication only.
    """
    def publish(target, **kwargs):
        target()
        return True

    with reuse_reporting_reader(store, monkeypatch) as reader:
        def snapshot(**kwargs):
            return nullcontext(SimpleNamespace(
                store=reader, release_reader=lambda: None, publish=publish,
                publish_ordered=publish, is_current=lambda: True, _changes=store.conn.total_changes))

        monkeypatch.setattr(store, 'read_snapshot', snapshot)
        yield


@contextmanager
def reuse_reporting_reader(store, monkeypatch):
    """Reuse one real file reader below tests of reporting consumers.

    Snapshot/publication tests keep their real private snapshot connections.
    Reader-pool admission and time budgets have separate owners.
    """
    from yt_playlist.core import schema_inspection

    assert store._db_path is not None
    with store._retained_readers():
        reader = store._make_reader()
        try:
            assert reader._read_only
            with monkeypatch.context() as patch, schema_inspection.reuse_tables(reader.conn):
                patch.setattr(store, 'read_view', lambda **kwargs: nullcontext(reader))
                yield reader
        finally:
            reader.conn.close()
