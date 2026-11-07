"""Installed settings fallbacks retain exact ranking dependencies during playback."""
import json

import pytest

from tests.core.test_predecessor_writes import installed
from tests.rec.test_model_refresh import seed
from yt_playlist.core import radio_provenance
from yt_playlist.rec import album_feedback_state, model_refresh, mode_surfaces


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_real_installed_rankings_publish_after_listening_index_invalidation(installed, monkeypatch):
    store, version = installed
    seed(store, distinct_albums=True)
    assert model_refresh.rebuild(store, 2000) is not None
    # Real older libraries retain these cached index markers. An empty fixture
    # misses the settings deletion performed by the public listening write.
    store.settings.set_setting('trend_first_play_source', 'canonical_listening')
    store.settings.set_setting('trend_first_play_watermark', 'captured')
    calculate = mode_surfaces._calculate_bundles
    ranked = []

    def during_playback(reader, now, policies):
        result = calculate(reader, now, policies)
        account = store.accounts.get_accounts()[0].id
        assert store.history.record_play_event(
            account, store.tracks.identity_key_for_video('v10'), 'v10', 150000)
        if version == 93:
            assert store.settings.get_setting('trend_first_play_source') is None
            assert store.settings.get_setting('trend_first_play_watermark') is None
        ranked.append(result[0])
        return result

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', during_playback)
    result = mode_surfaces.prepare_bundles(store, 2000)
    assert result is ranked[0]
    assert result['all']['wheelhouse']
    assert store.recommendations.head('home')['id'] == store.recommendations.head('fresh')['id']
    with store.read_view() as reader:
        assert mode_surfaces.assemble_cards(reader, 2001, 0)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def read_setting_owner(store, key):
    if key == 'album_feedback':
        return album_feedback_state.state(store)
    return store.playlists.radio_playlist_ids()


def setting_value(key, suffix):
    if key == 'album_feedback':
        return json.dumps({'release-' + suffix: {'kind': 'more', 'at': 1000}})
    if key == radio_provenance.KEYS[0]:
        return json.dumps(['radio-' + suffix])
    return 'radio-' + suffix


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('key', ['album_feedback', *radio_provenance.KEYS])
@pytest.mark.parametrize('change', ['insert', 'update', 'delete'])
def test_installed_feedback_and_radio_fallbacks_reject_changed_inputs(installed, key, change):
    store, _ = installed
    store.accounts.upsert_account('Main', 'main.json', None, True)
    if change != 'insert':
        store.settings.set_setting(key, setting_value(key, 'before'))
    with store.read_snapshot() as snapshot:
        read_setting_owner(snapshot.store, key)
        if change == 'delete':
            with store.conn:
                store.conn.execute('DELETE FROM settings WHERE key=?', (key,))
        else:
            store.settings.set_setting(key, setting_value(key, 'after'))
        assert not snapshot.publish(lambda: pytest.fail('Published changed feedback or Radio origins'))


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('key', ['album_feedback', radio_provenance.KEYS[0]])
def test_installed_fallback_readers_ignore_unrelated_index_settings(installed, key):
    store, _ = installed
    store.accounts.upsert_account('Main', 'main.json', None, True)
    store.settings.set_setting('trend_first_play_source', 'canonical_listening')
    with store.read_snapshot() as snapshot:
        read_setting_owner(snapshot.store, key)
        with store.conn:
            store.conn.execute("DELETE FROM settings WHERE key='trend_first_play_source'")
        assert snapshot.publish(lambda: True)
