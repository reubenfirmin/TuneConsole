"""Real SQL publication keeps last-good comparison output across file failures."""
import pytest

from yt_playlist.core.store import Store
from tests.conftest import initialize_consumer


@pytest.fixture
def file_store(tmp_path, _pristine_current_schema):
    result = Store(tmp_path / 'comparison.db')
    try:
        initialize_consumer(result, _pristine_current_schema)
        yield result
    finally:
        result.conn.close()


def test_native_comparison_retries_file_failure_and_survives_restart(file_store, monkeypatch):
    store = file_store
    surface = 'model_pair:99:incumbent:mixes'
    original = {'run_id': 99, 'data': {'tuning': 'original', 'menus': []}}
    replacement = {'run_id': 99, 'data': {'tuning': 'replacement', 'menus': []}}
    files = store.rec_proposals.comparisons.files
    publish = files.publish

    def outside_transaction(encoded):
        assert not store.conn.in_transaction
        return publish(encoded)

    monkeypatch.setattr(files, 'publish', outside_transaction)
    store.rec_proposals.put_proposals(surface, original, 10)
    before = store.conn.execute('SELECT publication_id FROM model_pair_heads').fetchone()[0]

    def disk_full(encoded):
        raise OSError('disk full')

    with monkeypatch.context() as patch:
        patch.setattr(files, 'publish', disk_full)
        with pytest.raises(OSError, match='disk full'):
            store.rec_proposals.put_proposals(surface, replacement, 11)
    assert store.conn.execute('SELECT publication_id FROM model_pair_heads').fetchone()[0] == before
    assert store.rec_proposals.get_proposals(surface) == original
    store.rec_proposals.put_proposals(surface, replacement, 11)
    assert store.rec_proposals.get_proposals(surface) == replacement
    assert not store.conn.in_transaction
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    restarted = Store(store._db_path)
    try:
        restarted.init_schema()
        assert restarted.rec_proposals.get_proposals(surface) == replacement
    finally:
        restarted.conn.close()


def test_native_radio_owns_its_published_ranked_generation(store):
    store.rec_ledger.record_serving_policy('radio-policy', {'algorithm': 'comparison'}, 1)
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 100)
    surface = 'model_pair:99:incumbent:radio'
    value = {'run_id': 99, 'data': {'policy_version': 'radio-policy', 'modes': [],
        'bundles': {'all': {'wheelhouse': [{'key': 'song|artist', 'pool_policy': 'radio-policy', 'score': .5}]}}}}
    store.rec_proposals.put_proposals(surface, value, 10)
    result = store.rec_proposals.get_proposals(surface)['data']
    generation = result['_ranked_generation']
    assert store.recommendations.head(surface + ':home')['id'] == generation
    assert store.recommendations.pool(surface + ':home', 'all', 'wheelhouse', generation=generation)['eligible_count'] == 1
    assert store.conn.execute('SELECT generation_id FROM model_pair_recommendation_roots').fetchone()[0] == generation
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
