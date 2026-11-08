"""Paired displays retain their own roots while Radio pages complete #486 ranks."""
import json
import sqlite3

import pytest

from tests.core.test_tv_state_migration import store, cutover
from yt_playlist.core import model_projections, recommendation_outputs


def test_paired_display_last_good_rollback_file_backup_and_explicit_policy_roots(store, tmp_path):
    store.rec_ledger.record_serving_policy('kept-policy', {'algorithm': 'original'}, 1)
    surface = 'model_pair:99:incumbent:mixes'
    artifact = store.rec_ledger.evidence().artifact({'at': 1})
    value = {'menus': [{'protos': [{'model_version': 'kept-policy', 'tracks': []}]}],
             'choice_context': {'artifact': artifact}, 'tuning': 'original'}
    store.rec_proposals.put_proposals(surface, {'run_id': 99, 'data': value}, 12)
    cutover(store)
    assert model_projections.read(store.conn, surface) == {'run_id': 99, 'data': value}
    assert tuple(store.conn.execute('SELECT run_ref,run_resolution,built_at FROM model_pair_publications').fetchone()) == (None, 'missing_run', 12)
    assert store.conn.execute('SELECT policy_version FROM model_pair_policies').fetchone()[0] == 'kept-policy'
    assert store.conn.execute('SELECT artifact FROM model_pair_contexts').fetchone()[0] == artifact
    prepared = model_projections.prepare(store.conn, surface, {'run_id': 99, 'data': {**value, 'tuning': 'replacement'}}, 13)
    with pytest.raises(InterruptedError), store.conn.atomic():
        model_projections.publish(store.conn, prepared, store=store)
        raise InterruptedError('Before both comparison arms commit')
    assert model_projections.read(store.conn, surface)['data'] == value
    with store.conn.atomic():
        manifest = recommendation_outputs.export(store.conn, tmp_path / 'output-backup')
    recommendation_outputs.restore(store.conn, manifest)
    assert model_projections.read(store.conn, surface)['data'] == value
    with store.conn.atomic():
        model_projections.clear(store.conn)
    assert model_projections.read(store.conn, surface) is None
    assert not store.conn.execute('SELECT 1 FROM model_pair_contexts').fetchone()
    assert not store.conn.execute('SELECT 1 FROM model_pair_policies').fetchone()


def test_interrupted_projection_import_retains_original_envelope_and_no_typed_prefix(store):
    surface = 'model_pair:2:incumbent:albums'
    value = {'run_id': 2, 'data': {'items': [], 'context': {}}}
    raw = json.dumps(value, indent=2)
    store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', (surface, raw, None))
    store.conn.commit()
    from yt_playlist.core.migrations import v095_recommendation_state as migration
    with pytest.raises(InterruptedError), store.conn.atomic():
        migration.apply(store.conn, None)
        raise InterruptedError('Before projection migration commit')
    assert tuple(store.conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()) == (raw, None)
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_pair_heads'").fetchone()


def test_radio_seed_and_saved_queue_policies_have_sql_custody_without_json_scanning(store, monkeypatch):
    from yt_playlist.core import radio_storage
    from yt_playlist.core import policy_config
    store.rec_ledger.record_serving_policy('retained-policy', {'algorithm': 'saved-radio'}, -10000)
    store.settings.set_setting('radio_seed_offer', json.dumps({'tracks': [
        {'video_id': 'video', 'serving_context': {'pool_policy': 'retained-policy'}}]}))
    cutover(store)
    monkeypatch.setattr(policy_config, 'references', lambda value: pytest.fail('Current cleanup scanned JSON'))
    candidates, _ = store.rec_ledger.policy_retention_candidates(100)
    assert 'retained-policy' not in candidates
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    state = {'episode_id': 'held-radio', 'identity': account, 'phase': 'playing', 'position': 0,
             'tracks': [{'video_id': 'video', 'title': 'Song', 'artist': 'Artist',
                         'serving_context': {'pool_policy': 'retained-policy'}}]}
    with store.conn.atomic():
        radio_storage.save(store.conn, state, expected=None, now=1)
    store.settings.delete_setting('radio_seed_offer')
    candidates, _ = store.rec_ledger.policy_retention_candidates(100)
    assert 'retained-policy' not in candidates
    with pytest.raises(sqlite3.IntegrityError), store.conn.atomic():
        store.conn.execute("DELETE FROM recommendation_policies WHERE version='retained-policy'")
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_current_bootstrap_and_restart_reject_generic_recommendation_writes(tmp_path):
    from yt_playlist.core.store import Store
    path = tmp_path / 'current-owners.db'
    store = Store(path)
    store.init_schema()
    try:
        with pytest.raises(sqlite3.IntegrityError, match='domain owner'), store.conn.atomic():
            store.conn.execute("INSERT INTO rec_proposals VALUES ('new-document-bucket','{}',1)")
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
    restarted = Store(path)
    try:
        restarted.init_schema()
        assert restarted._model_projections and restarted._clicked_library_pools
        assert restarted._tv_recommendations and restarted._model_comparison_state
    finally:
        restarted.conn.close()
