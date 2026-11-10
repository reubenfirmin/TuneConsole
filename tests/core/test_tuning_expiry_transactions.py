"""Expiry participates in the calculation caller's transaction."""
from contextlib import closing
import sqlite3

import pytest

from tests.conftest import FakeClient
from tests.core.test_predecessor_writes import installed
from yt_playlist.rec import mix_tuning, onboarding, surfaces, taste_analysis, taste_viz


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('calculation', ['fingerprint', 'transparency', 'cold', 'radio'])
def test_expired_tuning_keeps_caller_draft_and_expiry_in_its_transaction(installed, calculation):
    store, _ = installed
    with store.conn:
        store.conn.execute("INSERT INTO genre_family(key,name) VALUES ('expiry-family','Original family')")
        store.conn.execute("INSERT INTO genre_catalog(key,name,family_key,source) "
            "VALUES ('expiry-genre','Original genre','expiry-family','custom')")
    with mix_tuning.adjusting(store, 100):
        store.rec_model.set_lean('genre:expiry-family', 1.4, 100)
    before = store.rec_model.get_leans()
    now = 100 + mix_tuning.TTL_S + 1
    calculate = {
        'fingerprint': lambda: taste_analysis.taste_fingerprint(store, now),
        'transparency': lambda: taste_viz.model_transparency(store, now),
        'cold': lambda: surfaces.cold_candidates(store, now),
        'radio': lambda: onboarding.radio_sample(store, FakeClient(), now),
    }[calculation]
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            store.conn.execute("UPDATE genre_catalog SET name='Caller draft' WHERE key='expiry-genre'")
            calculate()
            assert store.conn.in_transaction
            assert not store.rec_model.get_leans()
            assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
            assert store.conn.execute("SELECT name FROM genre_catalog WHERE key='expiry-genre'").fetchone()[0] == 'Caller draft'
            with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as observer:
                assert observer.execute("SELECT name FROM genre_catalog WHERE key='expiry-genre'").fetchone()[0] == 'Original genre'
                assert observer.execute('SELECT count(*) FROM rec_lean').fetchone()[0] == len(before)
        finally:
            store.conn.rollback()
    assert store.rec_model.get_leans() == before
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) == '100'
    assert store.conn.execute("SELECT name FROM genre_catalog WHERE key='expiry-genre'").fetchone()[0] == 'Original genre'


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_failed_expiry_preserves_draft_and_retries_until_caller_commit(installed, monkeypatch):
    store, _ = installed
    with store.conn:
        store.conn.execute("INSERT INTO genre_family(key,name) VALUES ('expiry-family','Original family')")
    with mix_tuning.adjusting(store, 100):
        store.rec_model.set_lean('genre:expiry-family', 1.4, 100)
    before = store.rec_model.get_leans()
    now = 100 + mix_tuning.TTL_S + 1
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            store.conn.execute("UPDATE genre_family SET name='Caller draft' WHERE key='expiry-family'")
            with monkeypatch.context() as patch:
                def unavailable():
                    raise OSError('controlled expiry failure')
                patch.setattr(store.rec_model, 'clear_hidden_facets', unavailable)
                with pytest.raises(OSError, match='controlled expiry failure'):
                    mix_tuning.expire(store, now)
            assert store.conn.in_transaction
            assert store.rec_model.get_leans() == before
            assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) == '100'
            assert store.conn.execute("SELECT name FROM genre_family WHERE key='expiry-family'").fetchone()[0] == 'Caller draft'
            assert mix_tuning.expire(store, now)
            assert store.conn.in_transaction and not store.rec_model.get_leans()
            with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as observer:
                assert observer.execute("SELECT name FROM genre_family WHERE key='expiry-family'").fetchone()[0] == 'Original family'
                assert observer.execute('SELECT count(*) FROM rec_lean').fetchone()[0] == len(before)
            store.conn.commit()
            with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as observer:
                assert observer.execute("SELECT name FROM genre_family WHERE key='expiry-family'").fetchone()[0] == 'Caller draft'
                assert observer.execute('SELECT count(*) FROM rec_lean').fetchone()[0] == 0
        finally:
            store.conn.rollback()
