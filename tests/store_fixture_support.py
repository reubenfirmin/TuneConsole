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


def install_file_receipts(conn):
    from yt_playlist.core.destination_schema import statements

    for sql in statements():
        if sql.startswith(('CREATE TABLE collection_file_writes ', 'CREATE TABLE "collection_file_writes" ',
                           'CREATE UNIQUE INDEX ix_collection_file_address ',
                           'CREATE UNIQUE INDEX ix_collection_selected_', 'CREATE INDEX ix_collection_metadata_subject ',
                           'CREATE TRIGGER collection_file_contract_')):
            conn.execute(sql)


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
    fixture = next((name for name in ('installed', 'unadmitted_installed', 'guarded_installed')
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
    fixture = next((name for name in ('installed', 'unadmitted_installed', 'guarded_installed')
                    if name in request.fixturenames), None)
    if fixture is None:
        return
    store, _ = request.getfixturevalue(fixture)
    _publication_preparation_input(store, monkeypatch)


def _publication_preparation_input(store, monkeypatch):
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107


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

    with closing(sqlite3.connect(':memory:', factory=PristineSchema)) as schema:
        schema.execute('PRAGMA foreign_keys=ON')
        with pytest.MonkeyPatch.context() as patch:
            if entries is not None:
                bound_catalog(patch, entries=entries)
            _bootstrap_consumer_schema(schema, use_active_catalog=entries is not None)
        if snapshot_guards:
            # Revisions owns the transaction that batches these TEMP guards.
            schema.capture_revisions(Revisions(schema))
        schema.execute('PRAGMA query_only=ON')
        yield schema


def _bootstrap_consumer_schema(conn, *, use_active_catalog):
    import pytest
    from yt_playlist.core import destination_schema, genre_identity, schema_bootstrap
    from yt_playlist.core.migrations import registry, regular

    with pytest.MonkeyPatch.context() as patch, conn:
        if use_active_catalog:
            document = destination_schema.definition()
            seeds = [sql for sql in document['destination_seeds'] if not sql.startswith(
                ('INSERT INTO "genre_alias"', 'INSERT INTO "genre_catalog"', 'INSERT INTO "genre_family"'))]
            patch.setattr(destination_schema, 'definition', lambda: {**document, 'destination_seeds': seeds})
        conn.execute('BEGIN')
        schema_bootstrap.bootstrap(conn, registry.MIGRATIONS, regular.CHANGES, 1., validation=lambda conn: None)
        if use_active_catalog:
            genre_identity.seed_catalog(conn)


def bounded_consumer_schema(entries, *, snapshot_guards=True):
    return consumer_schema(entries=entries, snapshot_guards=snapshot_guards)


def initialize_consumer(store, pristine, *, snapshot_guards=True, repair_on_bind=True):
    assert pristine.execute('PRAGMA query_only').fetchone()[0] == 1
    pristine.backup(store.conn)
    store._bind_repositories()
    if repair_on_bind:
        repair.run_once(store)
    if store._db_path is not None:
        mode = store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        assert mode.lower() == 'wal'
    if snapshot_guards:
        store._snapshot_revisions = pristine.bind_revisions(store.conn)


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
    from yt_playlist.core.migrations.runner import RELEASE_VERSION

    assert version == RELEASE_VERSION, 'Pre-release schemas are reset rather than converted'
    bound_catalog(monkeypatch)
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz')), snapshot_guards=snapshot_guards) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=snapshot_guards) as database:
            yield database, version



@contextmanager
def mock_snapshot_coordinator(store, monkeypatch):
    """Real private SQL reads/writes below consumers of snapshot coordination.

    Conflict detection and source-change races use actual guarded snapshots in
    their own tests. This helper supplies uncontended publication only.
    """
    from yt_playlist.core.db_revisions import Reads, Revisions

    def publish(target, **kwargs):
        target()
        return True

    with reuse_reporting_reader(store, monkeypatch) as reader:
        reader._snapshot_reads = Reads()
        def snapshot(**kwargs):
            if getattr(store, '_snapshot_revisions', None) is None:
                store._snapshot_revisions = Revisions(store.conn)
            changes = store.conn.total_changes
            revisions = store._snapshot_revisions
            version = store.conn.execute('PRAGMA data_version').fetchone()[0]

            def is_current(*, allow_stale_tables=frozenset()):
                if (store._snapshot_revisions is not revisions
                        or store.conn.execute('PRAGMA data_version').fetchone()[0] != version):
                    return False
                # File admission reserves output paths without changing any
                # consumed catalog facts, as in the real guarded snapshots.
                return not any(revision > changes for (table, _), revision
                               in store._snapshot_revisions.capture().items()
                               if table not in allow_stale_tables | {'collection_file_writes'})

            return nullcontext(SimpleNamespace(
                store=reader, release_reader=lambda: None, publish=publish,
                publish_ordered=publish, is_current=is_current,
                _changes=changes, _schema=revisions.schema_version))

        monkeypatch.setattr(store, 'read_snapshot', snapshot)
        yield


@contextmanager
def reuse_reporting_reader(store, monkeypatch):
    """Reuse one real file reader below tests of reporting consumers.

    Snapshot/publication tests keep their real private snapshot connections.
    Reader-pool admission and time budgets have separate owners.
    """
    from yt_playlist.core import schema_inspection
    from yt_playlist.library.application_lifecycle import ApplicationLifecycle

    assert store._db_path is not None
    production_view = store.read_view
    with store._retained_readers():
        reader = store._make_reader()
        try:
            assert reader._read_only
            with monkeypatch.context() as patch, schema_inspection.reuse_tables(reader.conn):
                def read_view(**kwargs):
                    # Preparation caches are reused across actual admissions.
                    if kwargs.get('preparation'):
                        return production_view(**kwargs)
                    ApplicationLifecycle.attach_reader(store, reader)
                    return nullcontext(reader)

                patch.setattr(store, 'read_view', read_view)
                yield reader
        finally:
            reader.conn.close()
