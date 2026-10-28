import pytest

from yt_playlist.providers import credentials, deepseek, discogs, lastfm


@pytest.mark.parametrize('resolve,env_name,setting_name', [
    (lastfm.api_key, 'LASTFM_API_KEY', 'lastfm_api_key'),
    (discogs.token, 'DISCOGS_TOKEN', 'discogs_token'),
    (deepseek.api_key, 'DEEPSEEK_API_KEY', 'deepseek_api_key'),
])
def test_credential_precedence_and_blank_fallbacks(resolve, env_name, setting_name, store, tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text(f'{setting_name} = " toml "\n')
    store.settings.set_setting(setting_name, ' saved ')
    monkeypatch.setenv(env_name, ' environment ')
    assert resolve(store, config_path=path) == 'environment'
    monkeypatch.setenv(env_name, ' \t ')
    assert resolve(store, config_path=path) == 'saved'
    store.settings.set_setting(setting_name, ' ')
    assert resolve(store, config_path=path) == 'toml'
    assert resolve(config_path=path) == 'toml'
    for content in (f'{setting_name} = " "', f'{setting_name} = 42', 'invalid [ toml', ''):
        path.write_text(content)
        assert resolve(store, config_path=path) is None
    path.unlink()
    assert resolve(store, config_path=path) is None


def test_search_providers_require_credentials(store, tmp_path, monkeypatch):
    monkeypatch.delenv('LASTFM_API_KEY', raising=False)
    monkeypatch.delenv('DISCOGS_TOKEN', raising=False)
    monkeypatch.setattr(credentials.paths, 'config_path', lambda: tmp_path / 'absent.toml')
    assert not lastfm.available(store)
    assert not discogs.available(store)
    store.settings.set_setting('lastfm_api_key', 'key')
    assert lastfm.available(store)
    store.settings.set_setting('discogs_token', 'token')
    assert discogs.available(store)
