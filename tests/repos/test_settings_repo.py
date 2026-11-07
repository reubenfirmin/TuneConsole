"""DAO suite for SettingsRepo (key/value app settings)."""
import json

import pytest


def test_get_set_and_default(store):
    assert store.settings.get_setting("k") is None
    assert store.settings.get_setting("k", "fallback") == "fallback"
    store.settings.set_setting("k", "v")
    assert store.settings.get_setting("k") == "v"
    store.settings.set_setting("k", "v2")                # INSERT OR REPLACE
    assert store.settings.get_setting("k") == "v2"


def test_set_none_stores_empty_string(store):
    store.settings.set_setting("k", None)
    assert store.settings.get_setting("k") == ""


def test_setting_persists_on_the_shared_connection(store):
    store.settings.set_setting("lastfm_api_key", "abc")
    assert store.conn.execute("SELECT value FROM settings WHERE key='lastfm_api_key'").fetchone()[0] == "abc"
    assert store.settings.get_setting("lastfm_api_key") == "abc"


def test_namespace_lookup_treats_sql_pattern_characters_literally(store):
    for key in ("gc_!%:one", "gc_!%:two", "gcX!%:one", "gc_!anything:one", "other"):
        store.settings.set_setting(key, key)
    assert store.settings.get_settings_with_prefix("gc_!%:") == {
        "gc_!%:one": "gc_!%:one", "gc_!%:two": "gc_!%:two"}


def test_saved_output_file_failure_preserves_both_output_and_ordinary_settings(store, monkeypatch):
    store.settings.set_values({'rec_autotune_result': '{"ran_at": 10}', 'personal': 'before'})
    original = store.conn.execute('SELECT * FROM model_autotune_output').fetchone()
    def failed(encoded):
        assert not store.conn.in_transaction
        raise OSError('disk full')
    with monkeypatch.context() as patch:
        patch.setattr(store.settings.files, 'publish', failed)
        with pytest.raises(OSError, match='disk full'):
            store.settings.set_values({'rec_autotune_result': '{"ran_at": 11}', 'personal': 'after'})
    assert tuple(store.conn.execute('SELECT * FROM model_autotune_output').fetchone()) == tuple(original)
    assert store.settings.get_setting('rec_autotune_result') == '{"ran_at": 10}'
    assert store.settings.get_setting('personal') == 'before'
    assert not store.conn.in_transaction


def test_replaced_import_job_cannot_be_overwritten_after_its_file_write(store, monkeypatch):
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.settings.save_spotify_import_job({'status': 'running', 'account_id': account})
    completion = {**store.settings.spotify_import_job(), 'status': 'complete'}
    publish = store.settings.recovery.publish
    replaced = False
    def race(*args):
        nonlocal replaced
        assert not store.conn.in_transaction
        reference = publish(*args)
        if not replaced:
            replaced = True
            store.settings.save_spotify_import_job({'status': 'running', 'account_id': account, 'phase': 'import'})
        return reference
    with monkeypatch.context() as patch:
        patch.setattr(store.settings.recovery, 'publish', race)
        with pytest.raises(ValueError, match='replaced or updated'):
            store.settings.save_spotify_import_job(completion)
    current = store.settings.spotify_import_job()
    assert current['phase'] == 'import'
    assert current['status'] == 'running'
    assert current['_job_revision'] != completion['_job_revision']
    assert not store.conn.in_transaction


def test_transfer_revision_is_rechecked_after_its_file_write(store, monkeypatch):
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    initial = {'creation_phase': 'created', 'marker': 'marker', 'ytm_playlist_id': 'remote',
               'source_video_ids': ['first'], 'complete': False}
    store.settings.save_spotify_transfer(account, 'fingerprint', initial)
    pending = store.settings.spotify_transfer_receipts(account)['fingerprint']
    publish = store.settings.recovery.publish
    replaced = False

    def race(*args):
        nonlocal replaced
        assert not store.conn.in_transaction
        reference = publish(*args)
        if not replaced:
            replaced = True
            store.settings.save_spotify_transfer(account, 'fingerprint', {
                **pending, 'source_video_ids': ['first', 'second']})
        return reference

    with monkeypatch.context() as patch:
        patch.setattr(store.settings.recovery, 'publish', race)
        with pytest.raises(ValueError, match='stale revision'):
            store.settings.save_spotify_transfer(account, 'fingerprint', {**pending, 'complete': True})
    current = store.settings.spotify_transfer_receipts(account)['fingerprint']
    assert current['source_video_ids'] == ['first', 'second']
    assert current['complete'] is False
    assert current['marker'] == 'marker'
    assert current['ytm_playlist_id'] == 'remote'
    assert current['_receipt_revision'] != pending['_receipt_revision']
    assert not store.conn.in_transaction


def test_experiment_history_is_rechecked_after_its_evidence_write(store, monkeypatch):
    key = 'ppr_experiment_decisions'
    first = {'decision': 'retire', 'cohort': 'all', 'reason': 'first observation', 'at': 10,
             'powered': False, 'basis': 'observation', 'live_evidence': {'ppr': {}, 'cosine': {}}}
    store.settings.set_setting(key, json.dumps([first]))
    second = {**first, 'reason': 'newer observation', 'at': 11}
    stale = {**first, 'reason': 'stale observation', 'at': 12}
    publish = store.settings.recovery.publish
    appended = False

    def race(*args):
        nonlocal appended
        assert not store.conn.in_transaction
        reference = publish(*args)
        if not appended:
            appended = True
            store.settings.set_setting(key, json.dumps([first, second]))
        return reference

    with monkeypatch.context() as patch:
        patch.setattr(store.settings.recovery, 'publish', race)
        with pytest.raises(ValueError, match='history changed'):
            store.settings.set_values({key: json.dumps([first, stale]), 'personal': 'stale'})
    assert json.loads(store.settings.get_setting(key)) == [first, second]
    assert store.settings.get_setting('personal') is None
    assert not store.conn.in_transaction
