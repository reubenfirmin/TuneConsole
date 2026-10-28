"""Engineering contracts for shared consumers, not a listening-quality verdict."""
from copy import deepcopy
import json
import logging
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from tests.musical_support import prepare_profiles, prepare_vibes
from yt_playlist.core import catalog
from yt_playlist.util import genre_map
from yt_playlist.rec import (fresh_pool, home_cards, home_prepared, home_vibe, library_pool,
                             mix_tuning, mode_surfaces, musical_character, musical_profiles,
                             surfaces, vibe_pools)


@pytest.fixture
def opportunities(store):
    pools = {}
    for lane in ('library', 'fresh'):
        items = []
        for i in range(64):
            key = f'{lane}-{i}'
            good = i >= 40  # Outside an ordinary full mode bucket, not just a card.
            genre = ('Metal' if i % 2 else 'Techno') if good else 'Jazz'
            audio = {'energy': .05 if good else .95, 'mood_relaxed': .95 if good else .05}
            item = dict(key=key, video_id=key, title=key, artist=f'Artist {key}', album='',
                        genre=genre, score=.1 if good else 10, year='2000')
            if lane == 'library':
                seed_catalog_track(store, key, key, item['artist'], video_id=key, genre=genre, **audio)
            else:
                store.discovery.upsert_discovered_track(key, key, key, item['artist'], '', None,
                                                       genre, '2000', None, 1, audio=audio)
            items.append(item)
        pools[lane] = items
    store.rec_proposals.put_proposals('mode_bundles', {
        '0': {'wheelhouse': pools['library'][:40], 'fresh': pools['fresh'][:40]},
        'all': {'wheelhouse': pools['library'], 'explore': pools['library'], 'fresh': pools['fresh']},
        '_meta': {'full_pools': True, 'comfort_pool': 0}}, 1000)
    prepare_vibes(store)
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.set_setting(home_vibe.SETTING, 'relax')
    return pools


def test_fit_precedes_mode_heads_within_explicit_genres_in_every_cached_fallback(store, opportunities, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('A control must only assemble prepared opportunities')
    for name in ('for_you', 'explore_for_you', 'comfort_listening'):
        monkeypatch.setattr(surfaces, name, unexpected)
    monkeypatch.setattr(store.rec_query, 'track_audio_features', unexpected)
    monkeypatch.setattr(store.rec_query, 'library_keys', unexpected)
    monkeypatch.setattr(musical_profiles, 'resolve', unexpected)
    parameters = {'include_genres': ['techno'], 'avoid_genres': ['metal'], 'lane': 'wheelhouse', 'epoch': 0}
    cards = mode_surfaces.assemble_cards(store, 1000, 0, include_genres=['techno'], avoid_genres=['metal'])
    assert {card['lane'] for card in cards} == {'wheelhouse', 'fresh'}
    groups = [card['tracks'] for card in cards]
    groups += [fresh_pool.pick(store, 1000, ['techno'], ['metal']), library_pool.pick(store, 1000, parameters)]
    service = home_cards.HomeCards(store, SimpleNamespace(latest=lambda *_: None), logging.getLogger(__name__),
                                   genre_options=[])
    groups += [service.built_library(parameters, 1000, [])['tracks'],
               service.built_fresh(parameters, 1000)['tracks']]
    for tracks in groups:
        assert len(tracks) == 12
        assert all(genre_map.family(track['genre']) == 'techno' for track in tracks)
        assert all(int(track['key'].split('-')[-1]) >= 40 for track in tracks)
        assert all(musical_character.intensity(track) < .5 for track in tracks), [
            (track['key'], musical_character.intensity(track)) for track in tracks]
    assert all(track['serving_context']['vibe'] == 'relax' for card in cards for track in card['tracks'])
    for card in cards:
        for track in card['tracks']:
            context = track['serving_context']
            assert context['musical_version'] == track['musical']['version']
            assert context['musical_digest'] == track['musical']['digest']
            value, support = musical_character.intensity_evidence(track)
            assert context['intensity'] == value
            assert context['intensity_support'] == support
            assert context['intensity_source'] == 'provider_measurement'
            assert context['intensity_vibe'] in ('energize', 'release')
            fit = track['musical']['vibes']['fits'][context['intensity_vibe']]
            assert (value, support) == (fit['score'], fit['support'])


def test_prepared_profile_stamp_survives_a_new_profile_until_menu_publication(store, opportunities):
    before = vibe_pools.pick(store, 1000, 'relax', 'wheelhouse')
    track = before[0]
    tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .99)
    prepare_profiles(store)
    current = musical_character.load(store, [track['key']])[track['key']]
    assert current['digest'] != track['musical']['digest']
    after = vibe_pools.pick(store, 1000, 'relax', 'wheelhouse')
    assert after == before
    observed = {}
    from yt_playlist.rec import journeys
    def inspect(items, journey, seed, feat):
        observed.update({item['key']: feat(item)['energy'] for item in items})
        return items
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(journeys, 'journey_order', inspect)
        home_cards._order_by_journey(store, after, 'warm_up', 1)
    assert observed[track['key']] == musical_character.intensity(track)


def test_selected_genre_has_reserved_candidates_beyond_the_global_vibe_head(store):
    items = []
    for i in range(vibe_pools.POOL_LIMIT + 24):
        key = f'genre-reserve-{i}'
        genre = 'Techno' if i < vibe_pools.POOL_LIMIT else 'Jazz'
        store.discovery.upsert_discovered_track(key, key, key, f'Artist {i}', '', None, genre,
            None, None, 1, audio={'energy': .04 if genre == 'Techno' else .2, 'mood_relaxed': .95})
        items.append({'key': key, 'video_id': key, 'title': key, 'artist': f'Artist {i}', 'genre': genre})
    store.rec_proposals.put_proposals('mode_bundles', {'all': {'fresh': items}})
    prepare_vibes(store)
    assert all(t['genre'] == 'Techno' for t in vibe_pools.candidates(store, 'relax', 'fresh'))
    focused = vibe_pools.pick(store, 1000, 'relax', 'fresh', include_genres=['jazz'])
    other = vibe_pools.pick(store, 1000, 'relax', 'fresh', avoid_genres=['techno'])
    assert len(focused) == len(other) == 12
    assert all(t['genre'] == 'Jazz' for t in focused + other)
    # A thin or exhausted explicit choice must not silently expand to other genres.
    assert vibe_pools.pick(store, 1000, 'relax', 'fresh', include_genres=['metal']) == []


def test_unknown_intensity_stays_neutral_and_weaker_than_a_known_endpoint_fit():
    from yt_playlist.rec import radio_intensity
    unknown, gentle, intense = {'key': 'unknown'}, sound('gentle', energy=.1), sound('intense', energy=.9)
    assert radio_intensity.narrow([unknown, gentle, intense], 0) == [gentle]
    assert radio_intensity.narrow([unknown, gentle, intense], 1) == [intense]
    assert radio_intensity.narrow([unknown, gentle, intense], .5) == [unknown, gentle, intense]
    assert musical_character.provenance(unknown)['intensity'] is None


def test_live_and_reused_offers_preserve_current_explicit_exclusions(store, opportunities):
    pool = vibe_pools.candidates(store, 'relax', 'wheelhouse')
    first, second, third = pool[:3]
    store.rec_model.record_dislike(first['key'], 2000, 1000)
    store.rec_model.set_weight(f"genre:{genre_map.family('Metal')}", 0, lo=0, now=1000)
    tid = store.tracks.track_ids_for_videos([second['video_id']])[second['video_id']]
    with store.conn:
        catalog.choose_field(store.conn, tid, 'duration_s', 100_000)
    tid = store.tracks.track_ids_for_videos([third['video_id']])[third['video_id']]
    store.tracks.set_track_genre(tid, 'Classical')
    remaining = vibe_pools.pick(store, 1000, 'relax', 'wheelhouse')
    assert not {first['key'], second['key'], third['key']} & {track['key'] for track in remaining}
    assert all(track['genre'] != 'Metal' for track in remaining)
    protos = home_prepared._eligible_protos(store, [{'lane': 'wheelhouse', 'label': 'Familiar',
                                                   'name': 'Familiar - Test', 'tracks': deepcopy(pool[:24]),
                                                   'recipe': {'vibe': 'relax'}}], 1000)
    assert protos
    assert not {first['key'], second['key'], third['key']} & {t['key'] for p in protos for t in p['tracks']}
    assert all(t['genre'] != 'Metal' for p in protos for t in p['tracks'])


def sound(key, score=0, **overrides):
    return {'key': key, 'score': score, 'musical': musical_character.compact(
        musical_profiles.resolve([], overrides=overrides))}


@pytest.mark.parametrize('preset,preferred,other', [
    ('relax', {'instrumental': 1, 'mood_relaxed': 1, 'energy': .1},
              {'instrumental': 1, 'mood_aggressive': 1, 'energy': 1}),
    ('concentrate', {'instrumental': 1, 'energy': .3}, {'instrumental': 0, 'energy': .9}),
    ('cruise', {'energy': .5}, {'energy': .05}),
    ('energize', {'mood_happy': .1, 'energy': 1}, {'mood_happy': 1, 'energy': .1}),
    ('uplift', {'mood_happy': 1, 'energy': .1}, {'mood_happy': .1, 'energy': 1}),
    ('release', {'mood_aggressive': 1, 'energy': .1}, {'mood_aggressive': 0, 'energy': 1}),
])
def test_neighboring_presets_use_distinct_character_dimensions(preset, preferred, other):
    assert home_vibe.affinity(sound('preferred', **preferred), preset) > home_vibe.affinity(sound('other', **other), preset)


def test_actual_taste_scores_break_close_fit_ties_independently_of_pool_order_and_size():
    weak = sound('weak', score=100, energy=1)
    low = sound('low', score=-2, energy=.1)
    high = sound('high', score=2, energy=.11)
    assert sorted([weak, low, high], key=lambda item: home_vibe.order_key(item, 'relax')) == [high, low, weak]
    assert home_vibe.taste_score(high) == home_vibe.taste_score({**high, 'key': 'in-another-pool'})
    assert home_vibe.taste_score({'key': 'unknown'}) == .5


def test_clicked_library_fallback_prepares_the_shared_pools(store, monkeypatch):
    store.settings.set_setting(home_vibe.SETTING, 'release')
    calls = []
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', lambda s, now: calls.append((s, now)))
    library_pool.prepare(store, 1000, {'lane': 'explore', 'include_genres': ['jazz'], 'avoid_genres': []})
    assert calls == [(store, 1000)]


def test_prepared_set_from_another_vibe_is_served_with_a_preference_notice(store, opportunities):
    payload = home_prepared.prepare(store, 1000)
    assert payload and payload['menus']
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__), genre_options=[])
    assert home_prepared.cards(store, payload, 1000, service, record=False).protos
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    scheduled = []
    fallback = home_prepared.cards(store, payload, 1000, service, lambda: scheduled.append(True), record=False)
    assert fallback.protos and fallback.preparing
    assert fallback.preference_notice == "Heads up - we're unable to create energize mix tapes right now."
    assert all(p['recipe'].get('vibe') != 'energize' for p in fallback.protos)
    assert scheduled  # Partial menus and stale tuning can each request the same coalesced preparation.
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.delete_setting(home_vibe.SETTING)
    assert home_prepared.cards(store, payload, 1000, service, record=False).protos


def test_radio_midpoint_is_neutral_and_saved_session_preserves_profile_stamps(store, opportunities):
    from yt_playlist.rec import radio_tuning
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.web import radio_persistence
    from yt_playlist.web.radio_state import Phase, Session
    from yt_playlist.rec.radio_directions import RadioDirections
    c = RadioCandidates(store, 1000, radio_tuning.DEFAULTS)
    track = c._load('local')[0]
    genres = SimpleNamespace(breadth=.5, bonus=lambda _: 0)
    low, high = sound('low', energy=0), sound('high', energy=1)
    assert c._score({**track, 'musical': low['musical']}, [], None, {}, genres) == c._score(
        {**track, 'musical': high['musical']}, [], None, {}, genres)
    s = Session(object(), c, 1, 'On Air', phase=Phase.PLAYING, playlist='PL', tracks=[track],
                position=0, furthest=0, heard=[track])
    s.directions = RadioDirections(c, track)
    s.directions.update([track], [], 0)
    state = json.loads(radio_persistence.encode(s))
    before = deepcopy(state)
    newer = RadioCandidates(store, 1001, radio_tuning.DEFAULTS, now_source=c)
    restored = radio_persistence.restore(state, object(), 1, newer)
    assert restored.tracks == before['tracks']
    assert restored.directions.slots == before['directions']['slots']
    assert restored.tracks[0]['musical']['version'] == musical_character.VERSION
    assert restored.position == 0 and restored.playlist == 'PL'
