"""Saved recipe provenance is independent of today's Home controls."""
import json

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, home_pages_only, without_metadata_providers
from tests.playlist_fixture_support import remote_catalog
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as file_backed_store
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, home_vibe
from yt_playlist.web.app import create_app
from yt_playlist.web.playlist_heading import build_vibe_label, generated_heading


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


@pytest.mark.parametrize('preset', home_vibe.PRESETS)
def test_vibe_title_keeps_generated_type_date_and_version(preset):
    label = home_vibe.PRESETS[preset]['label']
    title = f'More in your wheelhouse - {label} - November 3 2026 #2'
    assert generated_heading(title, is_generated=True, recipe={'vibe': preset}) == {
        'name': 'More in your wheelhouse', 'kind': 'More in your wheelhouse',
        'date': '2026-11-03', 'date_label': 'November 3, 2026', 'version': '2'}
    assert build_vibe_label({'vibe': preset}) == label
    assert generated_heading(title, is_generated=True) is None


SAVED_VIBES = [*home_vibe.PRESETS, None, 'historical']


def mix_input(store, preset):
    tracks = [{'key': 'song|artist', 'video_id': 'song', 'title': 'Song', 'artist': 'Artist', 'genre': 'house'}]
    if preset == 'historical':
        # A historical/custom title is not evidence of a Vibe in its recipe.
        data = {'name': 'More in your wheelhouse - Focus - January 1 1970',
                'tracks': json.dumps(tracks)}
        return tracks, data, data['name'], None
    else:
        proto = home_cards._proto(store, 'wheelhouse', 'More in your wheelhouse', tracks, 1, vibe=preset)
        recipe = {'model': 'vibe' if preset else 'mode', 'lane': 'wheelhouse', 'vibe': preset}
        data = {'name': proto['name'], 'tracks': json.dumps(tracks), 'recipe': json.dumps(recipe)}
        return tracks, data, proto['name'] + ' #1', preset


@pytest.mark.parametrize('preset', SAVED_VIBES)
def test_generated_mix_saves_build_time_title_and_recipe(
        file_backed_store, preset, home_pages_only, without_metadata_providers):
    store = file_backed_store
    account = store.accounts.upsert_account('main', 'cred', None, True)
    tracks, data, title, expected_vibe = mix_input(store, preset)
    provider = FakeClient()
    provider._catalog.update(remote_catalog(tracks))
    app = create_app(store, lambda: {account: provider}, now_fn=lambda: 1, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.post('/home/generate', headers={'HX-Request': 'true'}, data=data)
        assert response.status_code == 200
        url = response.headers['HX-Redirect']
        assert provider.created[-1][1] == title
    playlist = store.playlists.get_playlist(int(url.rsplit('/', 1)[1]))
    assert playlist.title == title
    recipe = store.playlist_recipes.get_recipe(playlist.playlist_scope_id)
    assert (recipe or {}).get('vibe') == expected_vibe


def saved_mix(store, preset):
    """Completed playlist input; creation has its own real HTTP owner above."""
    account = store.accounts.upsert_account('main', 'cred', None, True)
    tracks, data, title, expected_vibe = mix_input(store, preset)
    tid = store.tracks.track_ids_for_videos(['song']).get('song')
    if tid is None:
        seed_publication_subjects(store, tracks)
        tid = store.tracks.track_ids_for_videos(['song'])['song']
    pid = store.playlists.upsert_playlist(account, f'saved-{preset}', title, 1, 'hash', 1)
    store.playlists.set_playlist_tracks(pid, [tid])
    playlist = store.playlists.get_playlist(pid)
    store.playlists.mark_generated_playlist(playlist.playlist_scope_id)
    recipe = json.loads(data['recipe']) if 'recipe' in data else None
    if recipe is not None:
        store.playlist_recipes.set_recipe(playlist.playlist_scope_id, recipe, 1)
    return account, playlist, expected_vibe, recipe


@pytest.mark.parametrize('preset', ['concentrate', 'historical'])
def test_saved_build_time_recipe_survives_real_store_reopen(file_backed_store, preset):
    store = file_backed_store
    _, playlist, _, recipe = saved_mix(store, preset)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        reopened.settings.set_setting(home_vibe.SETTING, 'energize')
        assert reopened.playlists.get_playlist(playlist.id).title == playlist.title
        assert reopened.playlist_recipes.get_recipe(playlist.playlist_scope_id) == recipe
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('preset', SAVED_VIBES)
def test_saved_mix_page_keeps_build_time_vibe_after_controls_change(
        file_backed_store, preset, home_pages_only, without_metadata_providers):
    store = file_backed_store
    account, playlist, expected_vibe, _ = saved_mix(store, preset)
    store.settings.set_setting(home_vibe.SETTING, 'energize')
    app = create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 2, background_work=False)
    url = f'/playlist/{playlist.id}'
    with TestClient(app, base_url='http://127.0.0.1') as client:
        page = client.get(url)
        assert page.status_code == 200
        assert playlist.title in page.text
        if expected_vibe:
            label = home_vibe.PRESETS[expected_vibe]['label']
            assert f'aria-label="Build-time Vibe: {label}">{label}</span>' in page.text
        else:
            assert 'class="pl-vibe-badge"' not in page.text
        if expected_vibe == 'concentrate':
            store.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
            renamed = client.post(url + '/rename', data={'title': 'My saved mix'})
            assert renamed.status_code == 200
            assert 'Build-time Vibe: Focus' in renamed.text
            assert 'Build-time Vibe: Focus' in client.get(url).text
