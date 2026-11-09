import json
import sqlite3

import pytest

from tests.core.test_tv_state_migration import store, cutover
from tests.store_fixture_support import without_query_timing
from yt_playlist.core import recommendation_setting_outputs as owner, recommendation_outputs as files
from yt_playlist.core.migrations import v095_recommendation_state as migration


def test_seed_offer_and_historical_report_keep_exact_files_references_and_backup(store, tmp_path):
    store.tracks.upsert_track('video', 'Song', 'Artist', None, 100)
    store.rec_ledger.record_serving_policy('kept-policy', {'algorithm': 'seed'}, 1)
    seed = json.dumps({'menu_id': 'old-menu', 'tracks': [
        {'video_id': 'video', 'pool_policy': 'kept-policy'}, {'video_id': 'historical-video'}]}, indent=2)
    report = '{"ran_at": 12, "winner": {"method": "svd", "dim": 48}}'
    store.settings.set_setting('radio_seed_offer', seed)
    store.settings.set_setting('rec_autotune_result', report)
    with pytest.raises(InterruptedError), store.conn.atomic():
        migration.apply(store.conn, None)
        raise InterruptedError('Before saved-output commit')
    assert store.settings.get_setting('radio_seed_offer') == seed
    assert not owner.available(store.conn)
    cutover(store)
    assert store.settings.get_setting('radio_seed_offer') == seed
    assert store.settings.get_setting('rec_autotune_result') == report
    assert not store.conn.execute("SELECT 1 FROM settings WHERE key IN ('radio_seed_offer','rec_autotune_result')").fetchone()
    assert [row[0] for row in store.conn.execute('SELECT resolution_reason FROM radio_seed_subjects ORDER BY position')] == [None, 'missing_entry']
    assert store.conn.execute('SELECT observed_at FROM model_autotune_output').fetchone()[0] == 12
    with pytest.raises(sqlite3.IntegrityError), store.conn.atomic():
        store.conn.execute("DELETE FROM recommendation_policies WHERE version='kept-policy'")
    with pytest.raises(sqlite3.IntegrityError, match='domain owner'), store.conn.atomic():
        store.conn.execute("INSERT INTO settings VALUES ('radio_seed_offer','{}')")
    with store.conn.atomic():
        manifest = files.export(store.conn, tmp_path / 'backup')
    files.restore(store.conn, manifest)
    assert owner.get(store.conn, 'radio_seed_offer') == seed
    assert owner.get(store.conn, 'rec_autotune_result') == report
    store.settings.delete_setting('radio_seed_offer')
    assert owner.get(store.conn, 'radio_seed_offer') is None
    assert owner.get(store.conn, 'rec_autotune_result') == report
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_invalid_seed_conversion_retains_settings_and_prior_policy_roots(store):
    store.settings.set_setting('radio_seed_offer', '{"tracks":[{"pool_policy":"historical"}]}')
    original = list(map(tuple, store.conn.execute('SELECT * FROM settings ORDER BY key')))
    with pytest.raises(ValueError, match='video reference'), store.conn.atomic():
        migration.apply(store.conn, None)
    assert list(map(tuple, store.conn.execute('SELECT * FROM settings ORDER BY key'))) == original
    assert not owner.available(store.conn)


def test_public_settings_writes_join_the_outer_transaction_and_roll_back_together(store):
    cutover(store)
    with pytest.raises(ValueError, match='outer transaction'):
        owner.put(store.conn, {'rec_autotune_result': '{}'})
    store.settings.set_values({'rec_autotune_result': '{"ran_at":1}', 'test_setting': 'original'})
    with pytest.raises(InterruptedError), store.conn.atomic():
        store.settings.set_setting('rec_autotune_result', '{"ran_at":2}')
        store.settings.set_values({'radio_seed_offer': '{"tracks":[]}', 'test_setting': 'changed'})
        store.settings.delete_setting('rec_autotune_result')
        raise InterruptedError('Before settings publication commits')
    assert store.settings.get_setting('rec_autotune_result') == '{"ran_at":1}'
    assert store.settings.get_setting('radio_seed_offer') is None
    assert store.settings.get_setting('test_setting') == 'original'
    assert not store.conn.in_transaction
