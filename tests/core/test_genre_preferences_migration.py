"""Keyed preferences preserve disabled defaults and deliberate custom choices."""
import sqlite3

import pytest

from yt_playlist.core.migrations import migrate, registry, regular
from yt_playlist.core.migrations.runner import MigrationError, validate_version
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'genres.db')
    _pristine_installed_schema(84).backup(store.conn)
    store._bind_repositories()
    yield store
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
        reopened.init_schema()
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


def test_renamed_identity_keeps_preference_and_repeated_configuration(store):
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
def test_direct_sql_rejects_invalid_preferences_and_referenced_identity(store, sql):
    store.genres.configure_catalog()
    with pytest.raises(sqlite3.IntegrityError):
        with store.conn:
            store.conn.execute(sql)
