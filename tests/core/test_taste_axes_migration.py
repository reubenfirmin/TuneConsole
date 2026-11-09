"""Typed genre/family feedback survives alias edits without touching history."""
from contextlib import closing
import sqlite3

import pytest

from tests.genre_catalog_support import bound_shipped_catalog
from tests.store_fixture_support import install_numbered_input, native_sql_calls, without_query_timing
from yt_playlist.core import genre_identity
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.store import Store
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_signals import RecSignalsRepo


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL migration checks do not construct an HTTP application."""


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_upgrade_preserves_typed_axes_and_original_events(_pristine_installed_schema):
    # Alias conversion owns real SQL, events and continuing writes. It has no
    # reopen or file-publication contract, so use a private SQLite database.
    old = Store(':memory:')
    # The upgrade and its assertions run synchronously on one private Store.
    # Keep reads under its real lock instead of constructing reporting readers.
    with closing(old.conn), old._lock:
        _pristine_installed_schema(87).backup(old.conn)
        # Construct the two real SQL consumers, rather than every unrelated
        # controller/file owner in a fully bound application Store.
        model, signals = RecModelRepo(old), RecSignalsRepo(old)
        model.set_lean('genre:Drum And Bass', 1.4, 10)
        model.hide_facet('genre:jazz')
        signals.bump_theme('genre:Drum And Bass', 2, 10)
        signals.log_graduation('genre:Drum And Bass', 'play', 2, 1.1, 1.1, 10)
        signals.set_play_graduated_day('genre:jazz', '2026-10-29')
        signals.set_play_graduated_day('genre:Drum And Bass', '2026-10-28')
        signals.set_play_graduated_day('genre:Drum & Bass', '2026-10-29')
        signals.bump_theme('genre:Drum & Bass', -0.5, 11)
        # v088 owns conversion of string axes and their retained events. The
        # installed-genre contract separately exercises later archive upgrades.
        migrate(old.conn, registry=registry.MIGRATIONS[:88])
        # Keep the existing consumers across conversion. Their SQL resolves
        # typed axes from the live schema; unrelated Store owners need no rebind.
        assert model.get_lean('genre:drum bass') == 1.4
        assert model.hidden_facets() == {'genre:jazz'}
        assert signals.get_theme('genre:drum bass') == 1.5
        assert signals.get_play_graduated_day('genre:jazz') == '2026-10-29'
        assert signals.get_play_graduated_day('genre:drum bass') == '2026-10-29'
        assert old.conn.execute("SELECT count(*) FROM taste_axis_legacy WHERE table_name='rec_play_grad'").fetchone()[0] == 3
        assert old.conn.execute("SELECT genre_key FROM taste_axes WHERE kind='genre'").fetchone()[0] == 'drum bass'
        assert old.conn.execute("SELECT family_key FROM taste_axes WHERE kind='family'").fetchone()[0] == 'jazz'
        old.conn.execute("UPDATE genre_catalog SET name='Drums renamed' WHERE key='drum bass'")
        old.conn.commit()
        assert model.get_leans() == {'genre:drum bass': 1.4}
        assert signals.recent_graduations()[0]['axis'] == 'genre:drum bass'
        assert old.conn.execute("SELECT raw_axis FROM taste_axis_legacy WHERE table_name='rec_grad_log'").fetchone()[0] == 'genre:Drum And Bass'
        signals.bump_theme('genre:drum bass', 0.5, 12)
        assert signals.get_theme('genre:drum bass') == 2
        validate_version(old.conn, 88)


def test_new_axes_constraints_and_unknown_evidence(store):
    store.rec_model.set_lean('genre:Made Up Provider Text', 1.3, 10)
    row = store.conn.execute("SELECT kind,value,resolution_reason FROM taste_axes").fetchone()
    assert tuple(row) == ('unresolved_genre','Made Up Provider Text','unknown_genre')
    store.rec_model.set_weight('genre:jazz', 1.2, now=10)
    assert store.rec_model.get_weights(now=10)['genre:jazz'] == 1.2
    store.rec_signals.bump_theme('genre:synth pop', 3, 10)
    assert store.rec_signals.theme_rows()[0]['facet'] == 'genre:synth pop'
    for sql in (
        "INSERT INTO taste_axes(kind,genre_key) VALUES ('genre','absent')",
        "INSERT INTO taste_axes(kind,value) VALUES ('other','genre:jazz')",
        "INSERT INTO taste_axes(kind,genre_key,family_key) VALUES ('genre','synth pop','jazz')",
        "INSERT INTO rec_lean(axis_id,value) VALUES (999999,1)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                store.conn.execute(sql)


def test_alias_collision_in_current_axes_rolls_back_losslessly(
        tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # Only the alias-collision edge is under test. Supply its completed v87
    # input with the actual shipped spellings and families used by the case.
    bound_shipped_catalog(monkeypatch, ('synth pop', 'drum bass', 'jazz'))
    old = Store(tmp_path / 'collision.db')
    with closing(old.conn), old._lock:
        old.conn.execute('PRAGMA synchronous=OFF')
        install_numbered_input(old.conn, 87)
        assert [row[0] for row in old.conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')] == list(range(1, 88))
        assert genre_identity.resolve(old.conn, 'Drum And Bass') == ('drum bass', None)
        assert not old.conn.execute('PRAGMA foreign_key_check').fetchone()
        model = RecModelRepo(old)
        model.set_lean('genre:Synth Pop', 1.3, 10)
        model.set_lean('genre:synth pop', 1.4, 10)
        with pytest.raises(Exception, match='multiple legacy axes'):
            migrate(old.conn, registry=registry.MIGRATIONS)
        assert old.conn.execute('SELECT count(*) FROM rec_lean').fetchone()[0] == 2
        assert not old.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_axes'").fetchone()
