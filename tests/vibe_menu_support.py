"""Detached repository results below compact and full-profile Vibe selection."""
from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from yt_playlist.core import json_cache
from yt_playlist.rec import home_vibe, musical_character, musical_profiles, vibe_preparation, vibe_readiness_index


class PublishedMenus:
    def __init__(self, bundle):
        self.bundle = bundle
        self.index = json_cache.encode(vibe_readiness_index.prepare(bundle))

    def head(self, surface):
        return {'id': 1}

    def framing(self, surface, **kwargs):
        return {'vibe_version': home_vibe.VERSION, 'comfort_pool': 36, 'year_low': None}

    def output(self, surface, name):
        return {'vibe_readiness': self.index}

    def candidate_pages(self, surface, selections, *, generation, limit):
        pools = self.bundle['_vibes']['pools'][surface.removeprefix('vibe:')]
        return [{'tracks': deepcopy(pools[lane][after + 1:after + 1 + limit]),
                 'exhausted': after + 1 + limit >= len(pools[lane])}
                for _mode, lane, after in selections]

    def selected_candidates(self, surface, selections, *, generation):
        pools = self.bundle['_vibes']['pools'][surface.removeprefix('vibe:')]
        return [[deepcopy(item) for item in pools[lane] if item['key'] in keys]
                for _mode, lane, keys in selections]


@pytest.fixture
def vibe_selection():
    # The original 116 subjects, six presets, shared library reserves and full
    # profiles remain. Catalog acquisition, inference and publication are below
    # this boundary; both selection algorithms and profile hydration stay real.
    stamp = vibe_preparation.serving_prefix() + 'completed-provider-inputs'
    profiles = {}
    for soft in (True, False):
        attributes = {'energy': .1 if soft else .9, 'mood_relaxed': .9 if soft else .1,
                      'mood_happy': .8 if soft else .2}
        profile = musical_profiles.resolve([], selected=attributes)
        profile['vibes'] = {'inputs_version': stamp, 'fits': {
            preset: {'score': home_vibe.heuristic_affinity(profile, preset), 'support': .8,
                     'source': 'provider_measurement'} for preset in home_vibe.PRESETS}}
        profiles[soft] = profile
    lanes, by_key = {}, {}
    for lane, count in (('wheelhouse', 16), ('explore', 16), ('fresh', 48), ('comfort', 36)):
        lanes[lane] = []
        for index in range(count):
            key = f'{lane}-{index}'
            by_key[key] = deepcopy(profiles[index % 2 == 0])
            by_key[key]['source_versions'] = [sha256(key.encode()).hexdigest()]
            lanes[lane].append({'key': key, 'video_id': key, 'title': key,
                'artist': f'{lane} artist {index}', 'genre': 'Jazz', 'score': .8, 'duration_s': None,
                '_radio_fresh': lane == 'fresh', 'musical': musical_character.compact(by_key[key])})
    library = lanes['wheelhouse'] + lanes['explore']
    pools = {preset: {lane: sorted(library if lane in ('wheelhouse', 'explore') else items,
                                  key=lambda item: home_vibe.order_key(item, preset))
                      for lane, items in lanes.items()} for preset in home_vibe.PRESETS}
    bundle = {'_meta': {'comfort_pool': 36}, '_vibes': {
        'version': home_vibe.VERSION, 'pools': pools, 'genres': {}}}
    items = {item['key']: item for values in lanes.values() for item in values}
    owned = {key: item for key, item in items.items() if not item['_radio_fresh']}
    fresh = {key: item for key, item in items.items() if item['_radio_fresh']}
    store = SimpleNamespace(
        _recommendation_files=True, _read_only=True,
        settings=SimpleNamespace(get_setting=lambda key: None),
        recommendations=PublishedMenus(bundle),
        musical_profiles=SimpleNamespace(
            by_keys=lambda keys: {key: deepcopy(by_key[key]) for key in keys},
            readiness_state=lambda keys: (None, {})),
        vibe_predictions=SimpleNamespace(fit_scores=lambda keys, preset:
            {key: by_key[key]['vibes']['fits'][preset]['score'] for key in keys}),
        rec_eligibility=SimpleNamespace(suppressed_candidate_keys=lambda *args: set(),
            offer_metadata=lambda videos: {video: {'genre': 'Jazz'} for video in videos}),
        rec_model=SimpleNamespace(suppressed_keys=lambda *args, **kwargs: set(),
                                 get_weights=lambda **kwargs: {}, muted_artists=lambda: ()),
        rec_query=SimpleNamespace(group_candidates=lambda keys: {key: owned[key] for key in keys if key in owned}),
        discovery=SimpleNamespace(candidate_tracks_by_keys=lambda keys:
                                  {key: fresh[key] for key in keys if key in fresh}),
        genres=SimpleNamespace(feature_axes=lambda labels: {label: ('jazz', None) for label in labels}),
        memoized_read=lambda key, read: read())
    store.read_view = lambda **kwargs: nullcontext(store)
    return store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # Selection has no web application or periodic-sync lifecycle.
    yield
