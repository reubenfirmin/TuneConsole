"""Typed genre/family feedback survives alias edits without touching history."""
import sqlite3

import pytest

from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store


def test_upgrade_preserves_typed_axes_and_original_events(tmp_path):
    old = Store(tmp_path / 'axes.db')
    try:
        migrate(old.conn, registry=registry.MIGRATIONS[:87])
        old._bind_repositories()
        old.genres.configure_catalog()
        old.rec_model.set_lean('genre:Drum And Bass', 1.4, 10)
        old.rec_model.hide_facet('genre:jazz')
        old.rec_signals.bump_theme('genre:Drum And Bass', 2, 10)
        old.rec_signals.log_graduation('genre:Drum And Bass', 'play', 2, 1.1, 1.1, 10)
        old.rec_signals.set_play_graduated_day('genre:jazz', '2026-10-29')
        old.rec_signals.set_play_graduated_day('genre:Drum And Bass', '2026-10-28')
        old.rec_signals.set_play_graduated_day('genre:Drum & Bass', '2026-10-29')
        old.rec_signals.bump_theme('genre:Drum & Bass', -0.5, 11)
        migrate(old.conn, registry=registry.MIGRATIONS)
        old._bind_repositories()
        assert old.rec_model.get_lean('genre:drum bass') == 1.4
        assert old.rec_model.hidden_facets() == {'genre:jazz'}
        assert old.rec_signals.get_theme('genre:drum bass') == 1.5
        assert old.rec_signals.get_play_graduated_day('genre:jazz') == '2026-10-29'
        assert old.rec_signals.get_play_graduated_day('genre:drum bass') == '2026-10-29'
        assert old.conn.execute("SELECT count(*) FROM taste_axis_legacy WHERE table_name='rec_play_grad'").fetchone()[0] == 3
        assert old.conn.execute("SELECT genre_key FROM taste_axes WHERE kind='genre'").fetchone()[0] == 'drum bass'
        assert old.conn.execute("SELECT family_key FROM taste_axes WHERE kind='family'").fetchone()[0] == 'jazz'
        old.conn.execute("UPDATE genre_catalog SET name='Drums renamed' WHERE key='drum bass'")
        old.conn.commit()
        assert old.rec_model.get_leans() == {'genre:drum bass': 1.4}
        assert old.rec_signals.recent_graduations()[0]['axis'] == 'genre:drum bass'
        assert old.conn.execute("SELECT raw_axis FROM taste_axis_legacy WHERE table_name='rec_grad_log'").fetchone()[0] == 'genre:Drum And Bass'
        current.validate(old.conn)
    finally:
        old.conn.close()


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


def test_alias_collision_in_current_axes_rolls_back_losslessly(tmp_path):
    old = Store(tmp_path / 'collision.db')
    try:
        migrate(old.conn, registry=registry.MIGRATIONS[:87])
        old._bind_repositories()
        old.genres.configure_catalog()
        old.rec_model.set_lean('genre:Synth Pop', 1.3, 10)
        old.rec_model.set_lean('genre:synth pop', 1.4, 10)
        with pytest.raises(Exception, match='multiple legacy axes'):
            migrate(old.conn, registry=registry.MIGRATIONS)
        assert old.conn.execute('SELECT count(*) FROM rec_lean').fetchone()[0] == 2
        assert not old.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_axes'").fetchone()
    finally:
        old.conn.close()
