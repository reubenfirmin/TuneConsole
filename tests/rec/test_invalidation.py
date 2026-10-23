"""Completed track spaces can serve through a refresh; retired artist vocabularies cannot."""
import pytest

from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec import invalidation


@pytest.mark.parametrize('current', [False, True])
@pytest.mark.parametrize('change', ['catalog', 'artist'])
def test_identity_invalidation_policy_preserves_eligible_spaces_and_rollback_evidence(current, change):
    store = Store(':memory:')
    try:
        migrate(store.conn,registry=MIGRATIONS if current else MIGRATIONS[:33])
        track_tables = ('rec_vectors','rec_content_vectors','rec_discovered_content_vectors')
        artist_tables = ('rec_artist_vectors','rec_artist_content_vectors')
        with store.conn:
            for table in (*track_tables,*artist_tables):
                store.conn.execute(f'INSERT INTO {table} VALUES (?,?)',('key',b'vector'))
            store.conn.execute("INSERT INTO rec_proposals VALUES ('home','{}',1)")
            store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)',[
                ('rec_content_model','track model'),('rec_artist_content_model','artist model'),
                ('rec_artist_fit_inputs','artist inputs')])
            store.conn.execute("INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (1,'evidence',2)")
        with store.conn:
            if change=='artist':
                invalidation.artist_reconciled(store.conn)
            else:
                invalidation.catalog_changed(store.conn)
        count = lambda table: store.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if change=='artist':
            assert all(count(table)==1 for table in track_tables)
            assert all(count(table)==0 for table in (*artist_tables,'rec_proposals'))
            assert store.conn.execute("SELECT value FROM settings WHERE key='rec_content_model'").fetchone()[0]=='track model'
            assert not store.conn.execute("SELECT 1 FROM settings WHERE key IN ('rec_artist_content_model','rec_artist_fit_inputs')").fetchone()
        elif current:
            assert all(count(table)==1 for table in (*track_tables,*artist_tables,'rec_proposals'))
        else:
            assert all(count(table)==0 for table in (*track_tables,*artist_tables,'rec_proposals'))
        if current:
            assert store.conn.execute("SELECT 1 FROM rec_change_queue WHERE kind='refresh' AND item=''").fetchone()
        evidence = store.conn.execute('SELECT result_json,identity_version FROM rec_model_selection_runs').fetchone()
        assert tuple(evidence)==('evidence',0)
    finally:
        store.conn.close()
