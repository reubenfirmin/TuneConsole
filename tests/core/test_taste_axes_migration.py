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
