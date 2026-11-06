"""Completed track spaces can serve through a refresh; retired artist vocabularies cannot."""
import pytest
from tests.derived_rating_support import seed_dislike

from yt_playlist.core import embeddings
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.rec import invalidation


@pytest.mark.parametrize('change', ['model', 'artist'])
def test_refresh_retains_playable_menus_usage_and_tv_until_replacement(store, change):
    from tests.test_tv import Videos, prepare_pool
    from yt_playlist.rec import home_prepared, onboarding, tv

    previous_tv = prepare_pool(store, Videos(), now=1000)
    previous_mix = home_prepared.peek(store, 1000)
    serving = {surface: store.rec_proposals.get_proposals(surface)
               for surface in ('home_prepared', 'home_tuned')}
    usage = {'home_format': {'episodes': {'mixes': 2}}}
    store.rec_proposals.put_proposals('listening_choices', usage, 1000)
    assert onboarding.prepared_mixes_ready(store, 1000)
    with store.conn.atomic():
        if change == 'artist':
            invalidation.artist_reconciled(store.conn)
        else:
            invalidation.clear_model_display(store.conn)
    assert {surface: store.rec_proposals.get_proposals(surface) for surface in serving} == serving
    assert store.rec_proposals.get_proposals('home_choice') is None
    assert store.rec_proposals.choice_usage()['episodes'] == {'mixes': 2}
    assert tv.cached(store) == previous_tv
    assert onboarding.prepared_mixes_ready(store, 1001)
    key = previous_mix['menus'][0]['protos'][0]['tracks'][0]['key']
    seed_dislike(store, key, until=None, now=1002)
    assert not onboarding.prepared_mixes_ready(store, 1002)
    # Removing evidence explicitly still clears the completed outputs.
    with store.conn.atomic():
        invalidation.clear_display(store.conn)
        invalidation.clear_findings(store.conn)
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
    assert not store.rec_proposals.choice_usage()
    assert tv.cached(store) is None


@pytest.mark.parametrize('current', [False, True])
@pytest.mark.parametrize('change', ['catalog', 'artist'])
def test_identity_invalidation_policy_preserves_eligible_spaces_and_rollback_evidence(current, change, consumer_store):
    store = consumer_store() if current else Store(':memory:')
    try:
        if not current:
            migrate(store.conn, registry=MIGRATIONS[:33])
        track_tables = ('rec_vectors','rec_content_vectors','rec_discovered_content_vectors')
        artist_tables = ('rec_artist_vectors','rec_artist_content_vectors')
        model = '{"ncat":1,"cont":[]}'
        with store.conn:
            store.conn.execute("INSERT INTO recordings(identity_key) VALUES ('key')")
            store.conn.execute("INSERT INTO artists(id,name,provisional) VALUES (1,'Artist',1)")
            for table in (*track_tables,*artist_tables):
                key = '@artist:1' if table in artist_tables else 'key'
                if current:
                    embeddings.publish(store.conn, table, [(key, bytes.fromhex('0000803F'))], replace=True)
                else:
                    store.conn.execute(f'INSERT INTO {table} VALUES (?,?)',(key,bytes.fromhex('0000803F')))
            if not current:
                store.conn.execute("INSERT INTO rec_proposals VALUES ('home_choice','{\"marker\":\"artist-choice\"}',1)")
            if not current:
                store.conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)',[
                    ('rec_content_model',model),('rec_artist_content_model',model)])
            store.conn.execute("INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) VALUES (1,'evidence',2)")
        if current:
            store.settings.set_setting('rec_content_model', model)
            store.settings.set_setting('rec_artist_content_model', model)
        original_history = list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version')))
        original_subjects = list(map(tuple, store.conn.execute('SELECT id,identity_key FROM recordings')))
        original_evidence = list(map(tuple, store.conn.execute(
            'SELECT id,started_at,result_json FROM rec_model_selection_runs')))
        # Exercise the production invalidator on both fresh and upgraded state.
        # Current runtime writers operate only after the schema upgrade finishes.
        if not current:
            assert original_history[-1][0] == 33
            migrate(store.conn, registry=MIGRATIONS)
            store.init_schema()
        else:
            store.rec_proposals.put_proposals('home_choice', {'marker': 'artist-choice'}, 1)
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        assert history[:len(original_history)] == original_history
        assert list(map(tuple, store.conn.execute('SELECT id,identity_key FROM recordings'))) == original_subjects
        assert list(map(tuple, store.conn.execute(
            'SELECT id,started_at,result_json FROM rec_model_selection_runs'))) == original_evidence
        assert store.rec_proposals.get_proposals('home_choice') == {'marker': 'artist-choice'}
        vectors = {table: embeddings.rows(store.conn, table) for table in (*track_tables, *artist_tables)}
        assert all(vectors[table] == [('key', bytes.fromhex('0000803F'))] for table in track_tables)
        assert all(vectors[table] == [('@artist:1', bytes.fromhex('0000803F'))] for table in artist_tables)
        store.settings.set_setting('rec_artist_fit_inputs', 'artist inputs')
        with store.conn:
            if change=='artist':
                invalidation.artist_reconciled(store.conn)
            else:
                invalidation.catalog_changed(store.conn)
        count = lambda table: embeddings.count(store.conn, table)
        if change=='artist':
            assert all(count(table)==1 for table in track_tables)
            assert all(embeddings.rows(store.conn, table) == vectors[table] for table in track_tables)
            assert all(count(table)==0 for table in artist_tables)
            assert store.rec_proposals.get_proposals('home_choice') is None
            assert store.settings.get_setting('rec_content_model') == model
            assert store.settings.get_setting('rec_artist_content_model') is None
            assert store.settings.get_setting('rec_artist_fit_inputs') is None
        else:
            assert all(count(table)==1 for table in (*track_tables,*artist_tables))
            assert all(embeddings.rows(store.conn, table) == vectors[table] for table in vectors)
            assert store.rec_proposals.get_proposals('home_choice') == {'marker': 'artist-choice'}
        assert store.conn.execute("SELECT 1 FROM rec_change_queue WHERE kind='refresh' AND item=''").fetchone()
        evidence = store.conn.execute('SELECT result_json,identity_version FROM rec_model_selection_runs').fetchone()
        assert tuple(evidence)==('evidence',0)
        assert list(map(tuple, store.conn.execute(
            'SELECT id,started_at,result_json FROM rec_model_selection_runs'))) == original_evidence
        assert list(map(tuple, store.conn.execute(
            'SELECT * FROM schema_migrations ORDER BY version'))) == history
    finally:
        store.conn.close()
