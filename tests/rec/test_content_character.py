"""Character evidence and its published track and artist representations."""
import json

import numpy as np
import pytest

from tests.conftest import seed_catalog_track
from tests.rec.test_musical_profiles import source
from tests.rec.test_progressive import drain
from yt_playlist.controllers.musical_profiles import MusicalProfiles
from yt_playlist.library.suggestion_composition import suggestions
from yt_playlist.rec import (
    artist_model, content_character, content_model, embed, model_refresh, musical_profiles, vibe_joint_state,
)
from yt_playlist.rec.progressive import Preparation
from yt_playlist.rec.vibe_score_policy import joint_suffix, serving_prefix


def _estimated(profile, scope, support=.6, *, qualified=True):
    stamp = serving_prefix() + 'test-model'
    fit_stamp = stamp + '/' + joint_suffix(vibe_joint_state.config_identity()) if qualified else stamp
    profile['vibes'] = {'inputs_version': stamp, 'intensity_fits': {'energize': {
        'source': scope, 'score': .9, 'support': support, 'inputs_version': fit_stamp}}}
    return profile


def test_content_admits_original_and_qualified_missing_intensity_once():
    raw = source(bpm=120, mood_relaxed=.9, instrumental=.5)
    profile = musical_profiles.resolve([raw])
    track, original = content_character.evidence(_estimated(profile, 'artist'))
    assert track == original
    assert track['tempo'][0] == pytest.approx((120 - 30) / 270)
    assert track['instrumental'][0] == .5

    conflicted = musical_profiles.resolve([raw, source(provider='another', bpm=290)])
    assert content_character.evidence(conflicted)[0]['tempo'][1] < track['tempo'][1] / 2
    inherited = musical_profiles.resolve([source(scope='release', tags=[{'name': 'Ambient'}])])
    assert content_character.evidence(inherited) == ({}, {})
    assert content_character.evidence(_estimated(inherited, 'artist', qualified=False)) == ({}, {})
    merged = musical_profiles.resolve([source(tags=[{'name': 'Ambient'}]), raw])
    assert content_character.evidence(merged)[0]['relaxed'] == track['relaxed']

    track, original = content_character.evidence(_estimated(inherited, 'artist'))
    assert track == {'intensity': (.9, .3)} and not original
    assert content_character.evidence(_estimated(inherited, 'artist', .2))[0]['intensity'][1] < .3
    model = content_model.build_content_model({}, {}, policy=content_character.VERSION)
    assert content_model.encode_content(model, None, None, {'character': {}}) is None
    assert content_model.encode_content(model, None, None, {'character': {'instrumental': (.5, .4)}}) is not None


def test_artist_character_uses_distinct_original_recordings_and_coverage():
    originals = {'a': ({'intensity': (.2, .4)}, {'intensity': (.2, .4)}),
                 'b': ({'intensity': (.8, .8)}, {'intensity': (.8, .8)})}
    refs = {'a': ['artist', 'artist'], 'b': ['artist']}
    for key, scope in enumerate(('artist', 'artist', 'artist_genre', 'genre', 'genre')):
        key = str(key)
        refs[key] = ['artist']
        originals[key] = content_character.evidence(_estimated(musical_profiles.resolve([]), scope))
    profile = artist_model.profiles_from_tracks({}, originals, refs, {})['artist']
    value, support = profile['character']['intensity']
    assert value == pytest.approx((.2 * .4 + .8 * .8) / 1.2)
    assert support == pytest.approx(1.2 / 7)


def test_sparse_character_preserves_support_in_published_track_and_artist_neighbors(store, monkeypatch):
    profiles = {}
    for video, artist in (('seed', 'Seed artist'), ('strong', 'Strong artist'),
                          ('weak', 'Weak artist'), ('unobserved', 'Weak artist'),
                          ('inferred-low', 'Inferred artist'), ('inferred-high', 'Inferred artist')):
        key = f'{video}|{artist.lower()}'
        seed_catalog_track(store, key, video, artist, video_id=video)
        if video.startswith('inferred'):
            support = .25 if video == 'inferred-low' else 1.
            profiles[key] = _estimated(musical_profiles.resolve([]), 'artist', support)
        elif video != 'unobserved':
            profiles[key] = musical_profiles.resolve([], overrides={'intensity': .9})

    monkeypatch.setattr(MusicalProfiles, 'by_keys',
                        lambda _self, keys: {key: profiles[key] for key in keys if key in profiles})
    assert model_refresh.rebuild(store, 1000)

    tracks = dict(embed.neighbors_blended(store, 'seed|seed artist'))
    low, high = tracks['inferred-low|inferred artist'], tracks['inferred-high|inferred artist']
    assert 0 < low < high < tracks['strong|strong artist']
    assert high == pytest.approx(2 * low)
    items = suggestions(store, None).similar('seed', None, 1000)['items']
    videos = [item['video_id'] for item in items]
    assert videos.index('inferred-high') < videos.index('inferred-low')

    artists = dict(artist_model.artist_neighbors(store, 'Seed artist'))
    assert 0 < artists['weak artist'] < artists['strong artist']
    assert 'inferred artist' not in artists


def test_profile_only_updates_and_withdrawals_refresh_serving(store, monkeypatch):
    seed = 'seed|artist a'
    nearer = 'near|artist b'
    cold = 'cold|artist c'
    rid = seed_catalog_track(store, seed, 'Seed', 'Artist A', video_id='seed', genre='Techno')
    seed_catalog_track(store, nearer, 'Near', 'Artist B', video_id='near', genre='Techno')
    store.discovery.upsert_discovered_track(cold, 'cold', 'Cold', 'Artist C', '', None,
                                            'Techno', None, None, 1)
    cold_rid = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (cold,)).fetchone()[0]
    evidence = {seed: .2, nearer: .9, cold: .2}

    def recording_inputs(_profiles, keys, *, source_scopes=None):
        include_recordings = source_scopes is None or 'recording' in source_scopes
        return {key: ([source(entity=key, instrumental=evidence[key])]
                      if key in evidence and include_recordings else [],
                      {}, False) for key in keys}

    monkeypatch.setattr(MusicalProfiles, 'recording_inputs', recording_inputs)
    preparation = Preparation(store)
    drain(preparation)
    assert model_refresh.rebuild(store, 1000)
    preparation.completed_full(preparation.full_checkpoint(1000))
    first = store.rec_model.content_generation()
    artist_a = store.artists.key('Artist A')

    def artist_vector():
        return dict(store.rec_model.get_rec_artist_content_vectors())[artist_a]

    def similar():
        return suggestions(store, None).similar('seed', None, 1000)['items'][0]['video_id']

    previous_artist = artist_vector()
    assert similar() == 'cold'
    assert artist_model.artist_neighbors(store, 'Artist A', topn=1)[0][0] == 'artist c'
    before = store.rec_query.track_audio_features()
    evidence.update({seed: .9, cold: .1})
    for subject in (rid, cold_rid):
        store.prepared_content.requeue_recording(subject)
    drain(preparation, 1001)
    updated = store.rec_model.content_generation()
    assert updated['model'] == first['model']
    for pool, key in (('library', seed), ('discovered', cold)):
        assert dict(updated[pool])[key] != dict(first[pool])[key]
    assert similar() == 'near'
    assert store.rec_query.track_audio_features() == before
    assert preparation.full_due(2000)
    assert model_refresh.rebuild(store, 2000)
    assert artist_vector() != previous_artist
    assert artist_model.artist_neighbors(store, 'Artist A', topn=1)[0][0] == 'artist b'

    del evidence[seed]
    store.prepared_content.requeue_recording(rid)
    drain(preparation, 2001)
    withdrawn = store.rec_model.content_generation()
    model = json.loads(withdrawn['model'])
    expected = content_model.encode_content(model, 'Techno', None, {})
    np.testing.assert_array_equal(np.frombuffer(dict(withdrawn['library'])[seed], np.float32), expected)
    assert model_refresh.rebuild(store, 2001)
    assert artist_model.artist_content_profiles(store)[artist_a]['character'] == {}
