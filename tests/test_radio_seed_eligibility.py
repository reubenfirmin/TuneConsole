"""Saved Radio offers and requested starts follow current upload eligibility."""
from copy import deepcopy
from tests.enrichment_support import complete_entries
from tests.derived_rating_support import seed_dislike
from datetime import date
import json
import threading

import pytest

from tests.conftest import seed_catalog_track
from tests.test_onboarding_routes import _client
from yt_playlist.core import catalog
from yt_playlist.rec import account_growth, rec_params
from yt_playlist.rec.radio_candidates import RadioCandidates, eligible_seeds
from yt_playlist.web.radio_launch import RadioLaunch


def seeds(store):
    rec_params.set_param(store, 'max_track_minutes', 5)
    tracks = []
    for index in range(4):
        track = {'key': f'current radio {index}|artist {index}', 'video_id': f'current-radio-{index}',
                 'title': f'Current Radio {index}', 'artist': f'Artist {index}',
                 'duration': 180, 'genre': 'Jazz', 'option_id': f'original-choice-{index}'}
        seed_catalog_track(store, track['key'], track['title'], track['artist'],
                           video_id=track['video_id'], duration_s=180, genre='Jazz')
        tracks.append(track)
    return tracks


def invalidate(store, tracks, condition):
    with store.conn:
        for track in tracks:
            entry = store.conn.execute('SELECT id FROM catalog_entries WHERE video_id=?', (track['video_id'],)).fetchone()[0]
            if condition == 'unavailable':
                store.conn.execute('UPDATE youtube_items SET available=0 WHERE video_id=?', (track['video_id'],))
            elif condition == 'overlong':
                catalog.choose_field(store.conn, entry, 'duration_s', 9000)
            elif condition == 'feedback':
                seed_dislike(store, track['key'], until=None, now=1000)
            elif condition == 'artist':
                store.rec_model.record_feedback('for_you', 'artist:' + track['artist'], 'mute', now=1000)
            elif condition == 'genre':
                store.rec_model.set_weight('genre:' + store.genres.feature_axes(['Jazz'])['Jazz'][0], 0, lo=0, now=1000)
            elif condition == 'identity':
                track['key'] = 'missing|source identity'


@pytest.mark.parametrize('condition', ['unavailable', 'overlong'])
def test_cached_radio_offer_agrees_across_http_bootstrap_and_direct_growth(store, condition, monkeypatch):
    # Radio eligibility is the subject. Completed earlier stops and an available
    # TV queue are lower-layer inputs; their real producers and unlock order
    # have independent owners in test_growth_readiness/test_growth_destinations.
    from yt_playlist.rec import growth_readiness, onboarding_outputs, tv

    monkeypatch.setattr(onboarding_outputs, 'prepared_mixes_ready', lambda *a, **kw: True)
    for name in ('foundation', 'albums', 'discover'):
        monkeypatch.setattr(growth_readiness, name, lambda *a, **kw: True)
    monkeypatch.setattr(tv, 'queue', lambda *a, **kw: [{} for _ in range(tv.QUEUE_SIZE)])
    _, client = _client(store)
    tracks = seeds(store)
    # First sounds remains playable when every old offer becomes ineligible.
    # Its admission must not accidentally become the reason Radio is blocked.
    seed_catalog_track(store, 'keeper|artist', 'Keeper', 'Artist', video_id='keeper', duration_s=180)
    complete_entries(store, [row[0] for row in store.conn.execute('SELECT id FROM tracks')], 1000)
    offer = {'menu_id': 'original-radio-menu', 'tracks': tracks}
    encoded = json.dumps(offer)
    store.settings.set_setting('radio_seed_offer', encoded)
    controller = client.app.state.ctx.radio
    initial = client.get('/home/growth').json()
    assert initial['ready_formats']['radio'] and initial['ready_areas']['tv']
    assert len(controller.bootstrap()['seeds']) == 4
    invalidate(store, tracks[:1], condition)
    assert len(controller.bootstrap()['seeds']) == 3
    assert not client.get('/home/growth').json()['ready_formats']['radio']
    invalidate(store, tracks, condition)
    state = client.get('/home/growth').json()
    direct = account_growth.view(store, 1000)
    node = next(node for node in state['nodes'] if node['id'] == 'radio')
    assert not node['usable'] and not state['ready_formats']['radio']
    assert not direct['ready_formats']['radio'] and not state['ready_areas']['tv']
    assert controller.bootstrap()['seeds'] == []
    assert controller.bootstrap()['seedMenu'] == offer['menu_id']
    assert store.settings.get_setting('radio_seed_offer') == encoded
    # Real current playback remains usable independently of the old suggestions.
    active = {'seeds': tracks, 'seedMenu': offer['menu_id'], 'state': {'active': True}}
    assert account_growth.view(store, 1000, radio=active)['ready_formats']['radio']


@pytest.mark.parametrize('condition', ['feedback', 'artist', 'genre', 'identity'])
def test_cached_radio_offer_applies_current_exclusions_and_requires_source_identity(store, condition):
    tracks = seeds(store)
    original = deepcopy(tracks)
    assert [track['option_id'] for track in eligible_seeds(store, tracks, 1000)] == [track['option_id'] for track in tracks]
    invalidate(store, tracks, condition)
    assert not eligible_seeds(store, tracks, 1000)
    if condition != 'identity':
        assert tracks == original


@pytest.mark.parametrize('condition', ['eligible', 'unavailable', 'overlong'])
@pytest.mark.parametrize('cached', [False, True])
def test_requested_seed_rechecks_current_eligibility_even_when_already_loaded(store, condition, cached):
    identity = store.accounts.upsert_account('Main', 'unused', None, True)
    tracks = seeds(store)
    candidates = RadioCandidates(store, 1000, comparison=None)
    try:
        if not cached:
            candidates.by_video.pop(tracks[0]['video_id'], None)
        if condition != 'eligible':
            invalidate(store, tracks[:1], condition)
        launch = RadioLaunch(store, None, None, None, threading.RLock(), lambda: 1000)
        session = launch.resolve(candidates, object(), identity, date(2026, 11, 2),
                                 seed_video_id=tracks[0]['video_id'])
        selected = session.selected[0] if session else None
        if condition == 'eligible':
            assert selected and selected['video_id'] == tracks[0]['video_id']
        else:
            assert selected is None or selected['video_id'] != tracks[0]['video_id']
    finally:
        candidates.close()
