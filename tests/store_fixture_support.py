"""Bind ordinary consumers to private copies of a validated empty schema.

Admission, bootstrap, upgrade and repair tests construct or reopen real Stores.
Consumer fixtures reuse the immutable schema admitted by the session builder;
they retain real repositories, revision guards, repair and WAL configuration.
"""
from copy import deepcopy
from contextlib import closing, contextmanager, nullcontext
import sqlite3

import pytest

from yt_playlist.core import repair
from yt_playlist.core.db_revisions import Revisions


@pytest.fixture
def file_consumer_store(tmp_path, _pristine_current_schema, monkeypatch):
    """Private WAL input for consumers that do not own snapshot construction."""
    from yt_playlist.core.store import Store

    directory = tmp_path / 'consumer'
    directory.mkdir()
    database = Store(directory / 'consumer.db')
    with closing(database.conn):
        initialize_consumer(database, _pristine_current_schema)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture
def publication_consumer_reads(request, monkeypatch):
    """Reuse real reporting reads below installed publication workflows.

    Explicit snapshots, reopens, publication guards and final installed-format
    validation keep their own connections. Independent native-upgrade cases
    in the same module have no installed consumer to configure.
    """
    fixture = next((name for name in ('installed', 'unadmitted_installed')
                    if name in request.fixturenames), None)
    if fixture is None:
        yield
        return
    from yt_playlist.core import schema_inspection

    store, _ = request.getfixturevalue(fixture)
    with reuse_reporting_reader(store, monkeypatch), schema_inspection.reuse_tables(store.conn):
        yield


def assert_installed_consumer_input(store, version):
    """Check the actual admitted SQL and private guards beneath consumers."""
    from yt_playlist.core.migrations import registry, regular

    source = store._fixture_installed_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, store.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:version]]
    assert regular.validate(store.conn) == regular.validate(source.conn)
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
def bounded_consumer_schema(entries, *, snapshot_guards=True):
    """Shipped SQL with bounded genre inputs, for consumers below admission.

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
        seeds = [sql for sql in document['destination_seeds'] if not sql.startswith(
            ('INSERT INTO "genre_alias"', 'INSERT INTO "genre_catalog"', 'INSERT INTO "genre_family"'))]
        with pytest.MonkeyPatch.context() as patch, schema:
            bound_catalog(patch, entries=entries)
            patch.setattr(destination_schema, 'definition', lambda: {**document, 'destination_seeds': seeds})
            schema.execute('BEGIN')
            schema_bootstrap.bootstrap(schema, registry.MIGRATIONS, regular.CHANGES,
                                       1., validation=lambda conn: None)
            genre_identity.seed_catalog(schema)
        if snapshot_guards:
            schema.capture_revisions(Revisions(schema))
        schema.execute('PRAGMA query_only=ON')
        yield schema


def initialize_consumer(store, pristine, *, snapshot_guards=True):
    assert pristine.execute('PRAGMA query_only').fetchone()[0] == 1
    pristine.backup(store.conn)
    store._schema_compatibility = False
    store._rebuild_source_version = None
    store._schema_recovery_pending = False
    store._bind_repositories()
    repair.run_once(store)
    if store._db_path is not None:
        mode = store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        assert mode.lower() == 'wal'
    if snapshot_guards:
        store._snapshot_revisions = pristine.bind_revisions(store.conn)



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
def reuse_reporting_reader(store, monkeypatch):
    """Reuse one real file reader below tests of reporting consumers.

    Snapshot/publication tests keep their real private snapshot connections.
    Reader-pool admission and time budgets have separate owners.
    """
    assert store._db_path is not None
    with store._retained_readers():
        reader = store._make_reader()
        try:
            assert reader._read_only
            with monkeypatch.context() as patch:
                patch.setattr(store, 'read_view', lambda **kwargs: nullcontext(reader))
                yield reader
        finally:
            reader.conn.close()
