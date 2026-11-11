"""Saved recipe provenance is independent of today's Home controls."""
import json

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, home_pages_only, without_metadata_providers
from tests.playlist_fixture_support import remote_catalog
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls, reuse_reporting_reader,
    uncontended_file_consumer_store as file_backed_store, without_query_timing,
)
from yt_playlist.core import migrations, schema_inspection
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, home_vibe, onboarding
from yt_playlist.web.app import create_app
from yt_playlist.web.playlist_heading import build_vibe_label


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def provenance_routes(monkeypatch, home_pages_only):
    """Keep actual creation/detail routes and rendering below saved provenance."""
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home_generation, playlists

    monkeypatch.setattr(webapp, 'build_all',
                        lambda ctx: [module.build(ctx)
                                     for module in (app_update, audio_preview, home_generation, playlists)])
    # Setup readiness controls other playlist actions. It does not supply the
    # title, saved recipe, badge, adoption or rename checked here.
    monkeypatch.setattr(onboarding, 'initial_setup_pending', lambda *args: False)


@pytest.mark.parametrize('preset', home_vibe.PRESETS)
def test_build_vibe_uses_saved_recipe(preset):
    label = home_vibe.PRESETS[preset]['label']
    assert build_vibe_label({'vibe': preset}) == label


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


def test_created_mix_provenance_survives_restart_adoption_and_rename(
        file_backed_store, provenance_routes, without_metadata_providers, monkeypatch):
    store = file_backed_store
    account = store.accounts.upsert_account('main', 'cred', None, True)
    provider = FakeClient()
    tracks = [{'key': 'song|artist', 'video_id': 'song', 'title': 'Song', 'artist': 'Artist', 'genre': 'house'}]
    provider._catalog.update(remote_catalog(tracks))
    saved = []
    app = create_app(store, lambda: {account: provider}, now_fn=lambda: 1, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        for preset in SAVED_VIBES:
            _, data, title, expected_vibe = mix_input(store, preset)
            response = client.post('/home/generate', headers={'HX-Request': 'true'}, data=data)
            assert response.status_code == 200
            url = response.headers['HX-Redirect']
            assert provider.created[-1][1] == title
            playlist = store.playlists.get_playlist(int(url.rsplit('/', 1)[1]))
            recipe = json.loads(data['recipe']) if 'recipe' in data else None
            assert playlist.title == title
            assert store.playlist_recipes.get_recipe(playlist.playlist_scope_id) == recipe
            saved.append((url, expected_vibe, playlist, recipe))
    reopened = Store(store._db_path)
    try:
        reopened.conn.execute('PRAGMA synchronous=OFF')
        # This case reopens persisted titles and recipe files in an already
        # installed layout. Schema admission is a completed lower input;
        # opening the actual database, binding owners and repair remain live.
        with monkeypatch.context() as admitted:
            admitted.setattr(migrations, 'migrate', lambda *args, **kwargs: False)
            reopened.init_schema()
        reopened.settings.set_setting(home_vibe.SETTING, 'energize')
        app = create_app(reopened, lambda: {account: provider}, now_fn=lambda: 2, background_work=False)
        # Rendering consumes ordinary SQL/file reports after the real reopen.
        # Reader-pool admission is below saved recipe provenance.
        with (reuse_reporting_reader(reopened, monkeypatch), schema_inspection.reuse_tables(reopened.conn),
              TestClient(app, base_url='http://127.0.0.1') as client):
            for url, expected_vibe, playlist, recipe in saved:
                assert reopened.playlists.get_playlist(playlist.id).title == playlist.title
                assert reopened.playlist_recipes.get_recipe(playlist.playlist_scope_id) == recipe
                page = client.get(url)
                assert page.status_code == 200
                assert playlist.title in page.text
                if expected_vibe:
                    label = home_vibe.PRESETS[expected_vibe]['label']
                    assert f'aria-label="Build-time Vibe: {label}">{label}</span>' in page.text
                else:
                    assert 'class="pl-vibe-badge"' not in page.text
            url, _, playlist, _ = saved[1]  # Focus
            reopened.playlists.adopt_generated_playlist(playlist.playlist_scope_id)
            renamed = client.post(url + '/rename', data={'title': 'My saved mix'})
            assert renamed.status_code == 200
            assert 'Build-time Vibe: Focus' not in renamed.text
            assert 'Build-time Vibe: Focus' not in client.get(url).text
            assert reopened.playlist_recipes.get_recipe(playlist.playlist_scope_id)['vibe'] == 'concentrate'
    finally:
        reopened.conn.close()
