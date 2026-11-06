"""Saved recipe provenance is independent of today's Home controls."""
import json

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.playlist_fixture_support import remote_catalog
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, home_vibe
from yt_playlist.web.app import create_app
from yt_playlist.web.playlist_heading import build_vibe_label, generated_heading


@pytest.mark.parametrize('preset', home_vibe.PRESETS)
def test_vibe_title_keeps_generated_type_date_and_version(preset):
    label = home_vibe.PRESETS[preset]['label']
    title = f'More in your wheelhouse - {label} - November 3 2026 #2'
    assert generated_heading(title, is_generated=True, recipe={'vibe': preset}) == {
        'name': 'More in your wheelhouse', 'kind': 'More in your wheelhouse',
        'date': '2026-11-03', 'date_label': 'November 3, 2026', 'version': '2'}
    assert build_vibe_label({'vibe': preset}) == label
    assert generated_heading(title, is_generated=True) is None


def test_created_mix_provenance_survives_restart_adoption_and_rename(file_backed_store):
    store = file_backed_store
    account = store.accounts.upsert_account('main', 'cred', None, True)
    provider = FakeClient()
    tracks = [{'key': 'song|artist', 'video_id': 'song', 'title': 'Song', 'artist': 'Artist', 'genre': 'house'}]
    provider._catalog.update(remote_catalog(tracks))
    saved = []
    app = create_app(store, lambda: {account: provider}, now_fn=lambda: 1, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        for preset in [*home_vibe.PRESETS, None]:
            proto = home_cards._proto(store, 'wheelhouse', 'More in your wheelhouse', tracks, 1, vibe=preset)
            recipe = {'model': 'vibe' if preset else 'mode', 'lane': 'wheelhouse', 'vibe': preset}
            response = client.post('/home/generate', headers={'HX-Request': 'true'}, data={
                'name': proto['name'], 'tracks': json.dumps(tracks), 'recipe': json.dumps(recipe)})
            assert response.status_code == 200
            url = response.headers['HX-Redirect']
            title = proto['name'] + ' #1'
            assert provider.created[-1][1] == title
            saved.append((url, preset, title))
        # A historical/custom title is not evidence of a Vibe in its recipe.
        historical = client.post('/home/generate', headers={'HX-Request': 'true'}, data={
            'name': 'More in your wheelhouse - Focus - January 1 1970', 'tracks': json.dumps(tracks)})
        saved.append((historical.headers['HX-Redirect'], None,
                      'More in your wheelhouse - Focus - January 1 1970'))
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        reopened.settings.set_setting(home_vibe.SETTING, 'energize')
        app = create_app(reopened, lambda: {account: provider}, now_fn=lambda: 2, background_work=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            for url, preset, title in saved:
                page = client.get(url)
                assert page.status_code == 200
                assert title in page.text
                if preset:
                    label = home_vibe.PRESETS[preset]['label']
                    assert f'aria-label="Build-time Vibe: {label}">{label}</span>' in page.text
                else:
                    assert 'class="pl-vibe-badge"' not in page.text
            url, preset, _ = saved[1]  # Focus
            playlist = reopened.playlists.get_playlist(int(url.rsplit('/', 1)[1]))
            reopened.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
            renamed = client.post(url + '/rename', data={'title': 'My saved mix'})
            assert renamed.status_code == 200
            assert 'Build-time Vibe: Focus' in renamed.text
            assert 'Build-time Vibe: Focus' in client.get(url).text
    finally:
        reopened.conn.close()
