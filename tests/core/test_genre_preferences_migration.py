"""Keyed preferences preserve disabled defaults and deliberate custom choices."""
from contextlib import closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import _file_consumer_store, bounded_consumer_schema, native_sql_calls
from yt_playlist.core.migrations import migrate, registry, regular
from yt_playlist.core.migrations.runner import Context, MigrationError, validate_version
from yt_playlist.core import genre_identity, schema_admission, schema_definition
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL-only cases never start web lifespan or background sync.

    Override the global web fixture so a preference conversion does not import
    and construct the entire web application just to disable an unused worker.
    """


@pytest.fixture
def bounded_vocabulary(monkeypatch):
    bound_catalog(monkeypatch)


@pytest.fixture
def bounded_current_store(tmp_path, bounded_vocabulary, native_sql_calls):
    # Preference behavior owns the real SQL catalog and constraints. Admission
    # and the independent genre vocabulary are completed inputs to these cases.
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz')),
                                 snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as store:
            yield store


@pytest.fixture
def predecessor(tmp_path, bounded_vocabulary):
    store = Store(tmp_path / 'genres.db')
    try:
        # Start from the shipped literal installed-v79 definition, not today's
        # bootstrap with an older version label. Construct the five unrelated
        # predecessor changes from their real definitions in one fixture
        # transaction; the v85 runner still performs its own admission checks.
        with closing(sqlite3.connect(':memory:')) as source:
            source.execute('PRAGMA foreign_keys=ON')
            source.execute('BEGIN')
            for sql in schema_admission.reference()['statements']:
                source.execute(sql)
            source.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                [(item.version, item.name) for item in registry.MIGRATIONS[:79]])
            source.executemany('INSERT INTO settings VALUES (?,?)',
                               schema_definition.SEED_SETTINGS.items())
            for sql in schema_definition.INITIAL_STATE:
                source.execute(sql)
            genre_identity.seed_catalog(source)
            for item in registry.MIGRATIONS[79:84]:
                with schema_definition.migration_definitions(item.version):
                    item.apply(source, Context(1.))
                source.execute('INSERT INTO schema_migrations VALUES (?,?,1)',
                               (item.version, item.name))
            source.commit()
            source.backup(store.conn)
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


def test_upgrade_restart_and_seeding_keep_disabled_defaults_and_custom(predecessor):
    predecessor.genres.configure_catalog()
    predecessor.genres.remove_genre('Rock')
    predecessor.genres.add_genre('Local listening style')
    before = predecessor.genres.get_genre_whitelist()
    # Exercise the keyed-preference conversion itself, followed by ordinary
    # native-format admission. Later explicit upgrades have their own owners
    # in test_installed_genre_contract.
    migrate(predecessor.conn, registry=registry.MIGRATIONS[:85])
    predecessor._bind_repositories()
    predecessor.genres.configure_catalog()
    assert predecessor.genres.get_genre_whitelist() == before
    assert tuple(predecessor.conn.execute('SELECT enabled,origin FROM genre_preferences '
        "WHERE genre_key='rock'").fetchone()) == (0, 'default')
    assert not predecessor.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='genre_whitelist'").fetchone()
    validate_version(predecessor.conn, 85)
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        reopened.genres.configure_catalog()
        assert reopened.genres.get_genre_whitelist() == before
        regular.validate(reopened.conn)
    finally:
        reopened.conn.close()


def test_ambiguous_predecessor_retains_all_original_rows(predecessor):
    with predecessor.conn:
        predecessor.conn.executemany("INSERT INTO genre_catalog(key,name,source) VALUES (?,?,'custom')",
                                     [('one', 'First'), ('two', 'Second')])
        predecessor.conn.execute("INSERT INTO genre_alias VALUES ('first','First','two','custom')")
        predecessor.conn.execute("INSERT INTO genre_whitelist VALUES ('First')")
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='Ambiguous enabled genre'):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before


def test_renamed_identity_keeps_preference_and_repeated_configuration(bounded_current_store):
    store = bounded_current_store
    store.genres.configure_catalog()
    store.genres.remove_genre('Rock')
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Rock renamed' WHERE key='rock'")
        store.conn.execute("UPDATE genre_catalog SET name='Jazz renamed' WHERE key='jazz'")
    store.genres.configure_catalog()
    assert 'Rock renamed' not in store.genres.get_genre_whitelist()
    assert 'Jazz renamed' in store.genres.get_genre_whitelist()
    assert store.conn.execute("SELECT enabled FROM genre_preferences WHERE genre_key='rock'").fetchone()[0] == 0


@pytest.mark.parametrize('sql', [
    "INSERT INTO genre_preferences VALUES ('absent',1,'user')",
    "UPDATE genre_preferences SET enabled=7",
    "UPDATE genre_preferences SET enabled=0.5",
    "UPDATE genre_preferences SET origin='guess'",
    "DELETE FROM genre_catalog WHERE key='rock'",
])
def test_direct_sql_rejects_invalid_preferences_and_referenced_identity(bounded_current_store, sql):
    store = bounded_current_store
    store.genres.configure_catalog()
    with pytest.raises(sqlite3.IntegrityError):
        with store.conn:
            store.conn.execute(sql)
