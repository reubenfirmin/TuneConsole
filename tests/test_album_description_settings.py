from yt_playlist.providers import deepseek
from yt_playlist.rec import album_descriptions
from tests.web_support import _client


def test_settings_key_entry_is_visible_but_saved_key_is_never_echoed(store, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    client = _client(store, lambda: {})
    key = 'sk-SECRET-0123456789'
    response = client.post('/settings/deepseek', data={'key': key, 'enabled': 'on', 'daily_limit': '8'})
    assert response.status_code == 200 and 'settings saved' in response.text
    assert key not in response.text
    assert store.settings.get_setting('deepseek_api_key') == key
    assert deepseek.api_key(store) == key
    assert 'id="deepseek-api-key" type="text"' in response.text and 'value="8"' in response.text
    assert key not in client.get('/setup?tab=enrichment').text
    client.post('/settings/deepseek', data={'key': '', 'enabled': 'on', 'daily_limit': '12'})
    assert deepseek.api_key(store) == key
    assert album_descriptions.settings(store, 1)['daily_limit'] == 12


def test_key_save_matches_lastfm_flow_and_preserves_generation_preferences(store):
    client = _client(store, lambda: {})
    store.settings.set_values({'deepseek_daily_limit': '8', 'deepseek_album_descriptions_enabled': '0'})
    response = client.post('/settings/deepseek-key', data={'key': '  sk-test-0123456789  '})
    assert response.status_code == 204 and not response.content
    assert store.settings.get_setting('deepseek_api_key') == 'sk-test-0123456789'
    assert store.settings.get_setting('deepseek_daily_limit') == '8'
    assert store.settings.get_setting('deepseek_album_descriptions_enabled') == '0'
    response = client.post('/settings/deepseek-key', data={'key': 'invalid secret'})
    assert response.status_code == 400 and not response.content
    assert store.settings.get_setting('deepseek_api_key') == 'sk-test-0123456789'


def test_invalid_form_does_not_partially_save_or_echo_submitted_secret(store):
    client = _client(store, lambda: {})
    for form in ({'key': 'sk-SECRET-0123456789', 'daily_limit': '1000'},
                 {'key': 'sk-SECRET-0123456789', 'daily_limit': 'garbage'},
                 {'key': 'sk-SECRET with spaces', 'daily_limit': '20'}):
        response = client.post('/settings/deepseek', data=form)
        assert 'role="alert"' in response.text and 'SECRET' not in response.text
        assert store.settings.get_setting('deepseek_api_key') is None
        assert store.settings.get_setting('deepseek_daily_limit') is None


def test_remove_key_pauses_generation_and_preserves_paid_description(store):
    client = _client(store, lambda: {})
    store.settings.set_setting('deepseek_api_key', 'sk-SECRET-0123456789')
    store.rec_proposals.put_proposals('album_description:' + album_descriptions.identity({'artist': 'A', 'title': 'B'}),
        {'description': 'Saved prose.', 'model': deepseek.MODEL}, 1)
    response = client.post('/settings/deepseek', data={'action': 'disconnect'})
    assert 'saved key removed' in response.text
    assert not store.settings.get_setting('deepseek_api_key')
    assert not album_descriptions.settings(store, 1)['enabled']
    assert album_descriptions.read(store, {'artist': 'A', 'title': 'B'})['text'] == 'Saved prose.'


def test_settings_save_wakes_missing_notes_without_resetting_counts_or_success(store):
    client = _client(store, lambda: {})
    store.rec_proposals.publish_album_note('missing', {'retry_at': 999999, 'editorial_version': 6}, 1)
    store.rec_proposals.publish_album_note('ready', {'retry_at': 999999, 'editorial_version': 6,
        'description': {'kind': 'generated', 'text': 'Saved prose.'}}, 1)
    store.rec_proposals.put_proposals('album_description_usage', {'day': 0, 'requests': 20,
        'status': 'auth', 'retry_at': 999999}, 1)
    client.post('/settings/deepseek', data={'key': 'sk-FIXED-0123456789', 'enabled': 'on', 'daily_limit': '20'})
    notes = store.rec_proposals.get_proposals('album_notes')
    assert notes['missing']['retry_at'] == 0 and notes['ready']['retry_at'] == 999999
    assert album_descriptions.settings(store, 1)['requests_today'] == 20


def test_home_renders_saved_generated_copy_without_provider_label_or_paid_request(store, monkeypatch):
    from yt_playlist.providers import release_notes
    client = _client(store, lambda: {})
    album = {'browse_id': 'record', 'artist': 'Artist', 'title': 'Album', 'score': .9}
    store.rec_proposals.put_proposals('album_prepared', {'items': [album], 'context': {}, 'built_at': 1}, 1)
    store.rec_proposals.put_proposals('album_description:' + album_descriptions.identity(album), {
        'description': 'A saved album description with <script>unsafe markup</script>.',
        'model': deepseek.MODEL, 'status': 'ready'}, 1)
    def forbidden(*args):
        raise AssertionError('Rendering must not call providers')
    monkeypatch.setattr(deepseek, 'describe', forbidden)
    monkeypatch.setattr(release_notes, 'fetch', forbidden)
    response = client.get('/home/album-choices')
    assert response.status_code == 200 and 'A saved album description' in response.text
    assert 'AI description · DeepSeek Flash' not in response.text
    assert '&lt;script&gt;unsafe markup&lt;/script&gt;' in response.text
    assert '<script>unsafe markup</script>' not in response.text
    assert not store.rec_proposals.get_proposals('album_description_usage')
