"""Home keeps prepared display content while reporting reads see new commits."""
import pytest
import json
from copy import deepcopy
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, publish_menu, returning_listener, without_metadata_providers
from tests.store_fixture_support import (
    bounded_consumer_schema, file_consumer_store as file_backed_store,
    native_sql_calls, without_query_timing,
)
from tests.onboarding_support import publish_live_mix_inputs
from yt_playlist.core import recommendation_impressions
from yt_playlist.web.app import create_app
from yt_playlist.rec.home_prepared import ROTATING_CARDS
from yt_playlist.web.routes.home_views import HomeViews
from yt_playlist.web.spotify_import import load_job, save_job


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing',
                                    'composition_routes', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Composition consumes already installed SQL and completed menus. Keep
    # real revision guards and private readers for the metadata-update race.
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz'))) as schema:
        yield schema


@pytest.fixture
def composition_routes(monkeypatch, home_routes_only):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_vibes, home, setup

    # The import notice is part of this composition. Construct its actual
    # setup route together with Home, without unrelated feature routers.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_vibes, home, setup)])


def client_for(store):
    account = store.accounts.upsert_account('main', None, True)
    return TestClient(create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1000,
                                 background_work=False),
                      base_url='http://127.0.0.1')


def test_live_home_reads_metadata_changes_on_the_next_request(file_backed_store):
    store = file_backed_store
    tracks = publish_live_mix_inputs(store)
    returning_listener(store, 'mixes', now=1000)
    track = next(track for track in tracks if track['video_id'].startswith('live-wheelhouse-'))
    previous_head = store.recommendations.head('home')['id']
    with client_for(store) as client:
        first = client.get('/')
        assert first.status_code == 200
        assert f'data-title="{track["title"]}"' in first.text
        track_id = store.conn.execute('SELECT id FROM track_sources WHERE video_id=?', (track['video_id'],)).fetchone()[0]
        store.tracks.set_track_title(track_id, 'Corrected title')
        following = client.get('/')
        assert following.status_code == 200
        assert 'data-title="Corrected title"' in following.text
        assert f'data-title="{track["title"]}"' not in following.text
        assert store.recommendations.head('home')['id'] == previous_head


def test_failed_home_visit_does_not_advance_only_some_cards(file_backed_store, caplog):
    store = file_backed_store
    returning_listener(store, 'mixes', now=1000)
    store.conn.execute("CREATE TEMP TRIGGER interrupt_visit BEFORE INSERT ON settings "
                       f"WHEN NEW.key='{recommendation_impressions.PREFIX + recommendation_impressions.CARD}' "
                       "BEGIN SELECT RAISE(ABORT,'Interrupted visit accounting'); END")
    with client_for(store) as client:
        assert client.get('/').status_code == 200
    assert 'Interrupted visit accounting' in caplog.text
    assert {card: store.rec_ledger.card_views(card) for card in ROTATING_CARDS} == {
        card: 0 for card in ROTATING_CARDS}
    assert not store.conn.in_transaction
    store.conn.execute('DROP TRIGGER interrupt_visit')
    with client_for(store) as client:
        assert client.get('/').status_code == 200
    assert {card: store.rec_ledger.card_views(card) for card in ROTATING_CARDS} == {
        card: int(card in ('wheelhouse', 'explore', 'comfort', 'fresh', 'cards')) for card in ROTATING_CARDS}


def test_prepared_albums_and_stories_arrive_when_their_tab_opens(
        file_backed_store, home_routes_only, without_metadata_providers):
    from tests.onboarding_support import publish_personal_album
    from yt_playlist.rec import album_choices, album_tuning, choice_evidence, into_recently

    store = file_backed_store
    client = client_for(store)
    # This composition check consumes completed albums/articles for a returning
    # listener. Model fitting and the first-use journey have their own owners.
    publish_personal_album(store, count=2)
    returning_listener(store, 'vibes', now=1000)
    song = {'key': 'album track|artist', 'video_id': 'album-track',
            'title': 'Album track', 'artist': 'Artist'}
    store.rec_proposals.put_proposals(into_recently.SURFACE, {
        'schema': into_recently.SCHEMA, 'built_at': 1000, 'cards': [
            {'kind': 'artist', 'subject': f'artist:Artist {index}',
             'display': f'Artist {index}', 'title': f'Artist {index}',
             'extract': 'A complete prepared article.', 'thumbnail': '/static/icon.png',
             'url': 'https://en.wikipedia.org/wiki/Jazz', 'color': '#4269aa',
             'seed': index, 'genre': 'Jazz', 'depth': 0, 'songs': [song]}
            for index in range(3)]}, 1000)
    store.settings.set_setting('home_format', 'albums')
    with client:
        home = client.get('/')
        assert home.status_code == 200
        assert home.context['home_default_panel'] == 'album-choices'
        board = home.context['album_board']
        assert len(board['albums']) >= 2 and home.context['story_board'] is None
        assert 'id="home-album-choices"' in home.text
        assert 'aria-label="Into recently"' not in home.text
        recent = client.get('/?panel=recent&fragment=1')
        assert recent.status_code == 200
        assert len(recent.context['story_board']['groups']) == 3
        assert recent.context['album_board'] is None
        assert 'aria-label="Into recently"' in recent.text
        assert 'hx-trigger="home-area-open" hx-swap="innerHTML"' not in home.text.split(
            'data-dash="album-choices"', 1)[1].split('</section>', 1)[0]
        held = choice_evidence.opportunity(store, board['opportunity_id'])
        context = choice_evidence.resolve_context(store, {'artifact': held['context']})
        assert context['album_menu']['standing_defaults'] == album_tuning.defaults(store)
        ids = [album['browse_id'] for album in board['albums']]
        assert [option['id'] for option in held['options']] == ids
        # Existing fragment interactions reuse the same complete menu/context.
        fragment = client.get('/home/album-choices')
        assert fragment.status_code == 200
        assert fragment.context['opportunity_id'] == board['opportunity_id']
        assert [album['browse_id'] for album in fragment.context['albums']] == ids
        returning_listener(store, 'albums', now=1000)
        album_choices.set_feedback(store, ids[0], 'reject', 1000)
        following = client.get('/')
        assert ids[0] not in [album['browse_id'] for album in following.context['album_board']['albums']]
        assert following.context['album_board']['opportunity_id'] != board['opportunity_id']


def test_home_embeds_radio_display_metadata_without_serializing_frozen_model_inputs(file_backed_store, monkeypatch):
    from yt_playlist.web.radio_projection import home_view

    store = file_backed_store
    payload = publish_menu(store)
    returning_listener(store, 'mixes', now=1000)
    returning_listener(store, 'radio', now=1000)
    track = {**payload['menus'][0]['protos'][0]['tracks'][0], 'saved': True,
             'musical': {'observations': ['private-model-input'] * 2000},
             'serving_context': {'frozen': ['private-model-input'] * 2000}}
    bootstrap = {'state': {'active': False, 'playlist_id': 'saved-radio', 'position': 0,
        'history_count': 1, 'tracks': [track], 'history': [track],
        'directions': [{'id': 'stream', 'slot': 2, 'active': True, 'anchor': track,
                        'tracks': [track], 'decay_remaining': 2, 'decay_total': 3}],
        'tuning': {'comfort': .7, 'theme': .4, 'energy': .8}},
        'seeds': [track], 'seedMenu': 'seed-menu', 'playback': {'connected': False, 'now_playing': None}}
    before = deepcopy(bootstrap)
    expected = {key: value for key, value in track.items() if key not in ('musical', 'serving_context')}
    projected = home_view(bootstrap)
    assert projected['seeds'] == [expected]
    assert projected['state']['tracks'] == projected['state']['history'] == [expected]
    assert projected['state']['directions'] == [{**bootstrap['state']['directions'][0],
                                                'anchor': expected, 'tracks': [expected]}]
    assert projected['state']['tuning'] == bootstrap['state']['tuning']
    assert len(json.dumps(projected)) < len(json.dumps(bootstrap)) / 20
    projected['seeds'][0]['title'] = 'Only this display copy'
    assert bootstrap == before
    with client_for(store) as client:
        monkeypatch.setattr(client.app.state.ctx.radio, 'bootstrap', lambda **kwargs: bootstrap)
        mixes = client.get('/?panel=directions')
        assert mixes.status_code == 200 and 'seed-menu' not in mixes.text
        opening = client.get('/?panel=radio')
        assert opening.status_code == 200
        assert 'private-model-input' not in opening.text
        assert 'seed-menu' in opening.text
        assert 'saved-radio' in opening.text
    assert bootstrap == before


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_home_leaves_interrupted_import_recovery_to_its_notice_poll(file_backed_store, status):
    store = file_backed_store
    tracks = publish_live_mix_inputs(store)
    track = next(track for track in tracks if track['video_id'].startswith('live-wheelhouse-'))
    returning_listener(store, 'mixes', now=1000)
    client = client_for(store)
    previous = {'status': status, 'account_id': store.accounts.get_accounts()[0].id, 'started_at': 900}
    save_job(store, previous)
    with client:
        opening = client.get('/')
        assert opening.status_code == 200
        assert 'Spotify import is running.' not in opening.text
        assert f'data-title="{track["title"]}"' in opening.text
        assert load_job(store) == previous
        notices = client.get('/?panel=notices&fragment=1')
        assert 'Spotify import is running.' in notices.text
        assert 'hx-get="/import/spotify-library/notice"' in notices.text
        assert load_job(store) == previous
        recovered = client.get('/import/spotify-library/notice')
        assert recovered.status_code == 200
        assert 'Spotify import stopped.' in recovered.text
        assert load_job(store)['status'] == 'error'
        assert load_job(store)['started_at'] == 900
        assert load_job(store)['finished_at'] == 1000
        following = client.get('/')
        assert following.status_code == 200
        assert 'Spotify import stopped.' in following.text
