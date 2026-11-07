"""Home keeps prepared display content while reporting reads see new commits."""
import pytest
import json
from copy import deepcopy
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, publish_menu, returning_listener, without_metadata_providers
from yt_playlist.rec import home_cache, home_cards, home_prepared
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home import ROTATING_CARDS
from yt_playlist.web.routes.home_views import HomeViews
from yt_playlist.web.spotify_import import load_job, save_job


def client_for(store):
    account = store.accounts.upsert_account('main', 'cred', None, True)
    return TestClient(create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1000,
                                 background_work=False),
                      base_url='http://127.0.0.1')


def test_home_metadata_update_preserves_publication_and_refreshes_next_snapshot(file_backed_store, monkeypatch):
    store = file_backed_store
    payload = publish_menu(store, tracks_per_card=5)
    returning_listener(store, 'mixes', now=1000)
    track = payload['menus'][0]['protos'][0]['tracks'][0]
    original = HomeViews.prepared
    updated = False
    observed = []

    def update_after_menu_read(self, now, request=None):
        nonlocal updated
        result = original(self, now, request)
        if request is not None and request.url.path == '/':
            observed.append(self.store.tracks.metadata_for_video(track['video_id'])['title'])
            if not updated:
                updated = True
                store.tracks.upsert_track(track['video_id'], 'Corrected title', track['artist'], '', 180)
                assert self.store.tracks.metadata_for_video(track['video_id'])['title'] == 'Corrected title'
                assert not self.store.conn.in_transaction
        return result

    monkeypatch.setattr(HomeViews, 'prepared', update_after_menu_read)
    with client_for(store) as client:
        first = client.get('/')
        assert first.status_code == 200
        assert f'data-title="{track["title"]}"' in first.text
        assert 'data-title="Corrected title"' not in first.text
        assert observed[0] == track['title']
        assert observed[1:] and set(observed[1:]) == {'Corrected title'}
        observed.clear()
        following = client.get('/')
        assert following.status_code == 200
        assert observed and set(observed) == {'Corrected title'}
        assert f'data-title="{track["title"]}"' in following.text
        assert 'data-title="Corrected title"' not in following.text
        assert home_cache.read(store, home_prepared.SURFACE) == payload
        # An explicit build replaces the held menu, including its display metadata.
        replacement = deepcopy(payload['menus'][0])
        replacement['protos'][0]['tracks'][0]['title'] = 'Corrected title'
        assert home_prepared.remember(store, payload, home_cards.CardsResult(**replacement), 1001)
        refreshed = client.get('/')
        assert refreshed.status_code == 200
        assert 'data-title="Corrected title"' in refreshed.text
        assert f'data-title="{track["title"]}"' not in refreshed.text


def test_failed_home_visit_does_not_advance_only_some_cards(file_backed_store, monkeypatch):
    store = file_backed_store
    original = store.rec_ledger.bump_card_view

    def fail_partway(card, now):
        if card == 'fresh':
            raise RuntimeError('Interrupted visit accounting')
        return original(card, now)

    monkeypatch.setattr(store.rec_ledger, 'bump_card_view', fail_partway)
    with client_for(store) as client:
        with pytest.raises(RuntimeError, match='Interrupted visit accounting'):
            client.get('/')
    assert {card: store.rec_ledger.card_views(card) for card in ROTATING_CARDS} == {
        card: 0 for card in ROTATING_CARDS}


def test_prepared_albums_and_stories_arrive_in_the_home_document(
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
        stories = home.context['story_board']
        assert len(board['albums']) >= 2 and len(stories['groups']) == 3
        assert 'id="home-album-choices"' in home.text
        assert 'aria-label="Into recently"' in home.text
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
        opening = client.get('/')
        assert opening.status_code == 200
        assert 'private-model-input' not in opening.text
        assert 'seed-menu' in opening.text
        assert 'saved-radio' in opening.text
    assert bootstrap == before


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_home_leaves_interrupted_import_recovery_to_its_notice_poll(file_backed_store, status):
    store = file_backed_store
    publish_menu(store, tracks_per_card=5)
    returning_listener(store, 'mixes', now=1000)
    client = client_for(store)
    previous = {'status': status, 'account_id': store.accounts.get_accounts()[0].id, 'started_at': 900}
    save_job(store, previous)
    with client:
        opening = client.get('/')
        assert opening.status_code == 200
        assert 'Spotify import is running.' in opening.text
        assert 'hx-get="/import/spotify-library/notice"' in opening.text
        assert load_job(store)['status'] == status
        assert 'finished_at' not in load_job(store)
        assert 'data-title=' in opening.text
        recovered = client.get('/import/spotify-library/notice')
        assert recovered.status_code == 200
        assert 'Spotify import stopped.' in recovered.text
        assert load_job(store)['status'] == 'error'
        assert load_job(store)['started_at'] == 900
        assert load_job(store)['finished_at'] == 1000
        following = client.get('/')
        assert following.status_code == 200
        assert 'Spotify import stopped.' in following.text
