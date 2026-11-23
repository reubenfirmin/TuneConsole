"""Typed genre/family feedback survives alias edits without touching history."""
import sqlite3

import pytest

from tests.store_fixture_support import without_query_timing
from yt_playlist.core import taste_axes
from yt_playlist.rec import mix_tuning


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL checks do not construct an HTTP application."""


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
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
        "INSERT INTO taste_axes(kind,value,lean_updated_at) VALUES ('other','no-lean',1)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                store.conn.execute(sql)


def test_mix_publication_still_tracks_temporary_controls(store):
    axis = 'lane:deep_cut'
    store.rec_model.set_lean(axis, 1.3, 10)
    revision = store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS)
    store.rec_model.set_weight(axis, 1.5, now=10)
    store.rec_signals.bump_theme(axis, 1, 10)
    store.rec_signals.set_play_graduated_day(axis, '2026-11-21')
    assert store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS) == revision
    store.rec_model.hide_facet(axis)
    hidden = store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS)
    assert hidden != revision
    store.rec_model.clear_lean(axis)
    assert store.home_cache.input_revision(mix_tuning.PUBLICATION_INPUTS) != hidden
