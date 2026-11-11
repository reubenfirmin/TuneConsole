"""Controlled prepared inputs for Radio policy checks, without library preparation.

RadioCandidates and RadioDirections remain real. The publication reader, catalog,
profiles and vectors are collaborators; their SQL/file contracts have real owners
in test_stored_radio and test_typed_recommendation_publication.
"""
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from yt_playlist.rec import musical_character, radio_candidates, radio_intensity
from yt_playlist.rec.stored_radio import LOOKAHEAD, PreparedRadio


@pytest.fixture
def intensity_inputs(monkeypatch):
    inputs_state = SimpleNamespace(indexed=True)
    pools, modes, profiles, vectors = {}, {}, {}, {}
    labels = ['Electronic'] * 5 + ['Synthwave', 'Jazz', 'Rock', 'Ambient']
    for index, label in enumerate(labels):
        mid = str(index + 1)
        angle = index * .01 if index < 6 else (index - 5) * 1.2
        vector = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)
        modes[mid] = {'mode_id': index + 1, 'label': label, 'centroid': vector,
                      'families': [[label, 12]], 'space': 'controlled'}
        rows = []
        for offset in range(12 + (LOOKAHEAD + 14 if mid == '9' else 0)):
            key = f'policy-{mid}-{offset}'
            energy = .04 if mid == '9' and offset >= 12 else .96
            profile = {'version': musical_character.VERSION,
                       'dimensions': {'intensity': {'value': energy, 'support': 1}}}
            profiles[key] = profile
            vectors[key] = vector.tobytes()
            rows.append({'key': key, 'video_id': key, 'title': key,
                         'artist': 'Artist ' + key, 'genre': label, 'musical': profile})
        pools[mid] = rows
    quiet = pools['9'][12:]
    all_tracks = {track['key']: track for rows in pools.values() for track in rows}
    weights, reads = {}, []

    def axes(labels):
        return {label: (('Electronic' if label and label.casefold() == 'synthwave'
                        else (label or '').title()), (label or '').casefold()) for label in labels}

    store = SimpleNamespace(
        _recommendation_files=True, _lock=nullcontext(),
        settings=SimpleNamespace(get_setting=lambda name: {'rec_content_model': 'controlled',
            'rec_param:now_gain': '0'}.get(name)),
        rec_model=SimpleNamespace(disliked_identity_keys=set, suppressed_keys=lambda *args: set(),
            muted_artists=list, get_weights=lambda **kwargs: weights,
            content_vector_pools=lambda keys: {'library': {key: vectors[key] for key in keys},
                                               'discovered': {}}),
        genres=SimpleNamespace(feature_axes=axes),
        rec_proposals=SimpleNamespace(get_proposals=lambda surface: None),
        recommendations=SimpleNamespace(ranks=lambda *args, **kwargs: {},
            intensity_bands=lambda *args, **kwargs: [2, 48]),
        rec_query=SimpleNamespace(tracks_by_keys=lambda keys: {}),
        accounts=SimpleNamespace(get_accounts=list),
        radio_catalog=SimpleNamespace(details=lambda keys: {key: all_tracks[key] for key in keys},
            rotation_facts=lambda keys: {key: {'owned': not all_tracks[key].get('_radio_fresh'),
                'heard': bool(all_tracks[key].get('plays')), 'last_started': None, 'last_offered': None}
                for key in keys if key in all_tracks}))

    def intensity_pool(bands, mode, energy, *, allowed, genre_allowed, excluded, used, limit=LOOKAHEAD):
        assert 1 <= limit <= LOOKAHEAD
        reads.append((mode, limit, frozenset(used)))
        result, priority = [], None
        ordered = sorted(bands[mode], key=lambda value: radio_intensity.loss(value, energy))
        for value in ordered:
            loss = radio_intensity.loss(value, energy)
            if priority is not None and loss > priority:
                break
            for track in bands[mode][value]:
                if (track['key'] in used or track['video_id'] in excluded
                        or not genre_allowed(track['genre']) or not allowed(track)):
                    continue
                priority = loss
                result.append(track)
                if len(result) == limit:
                    return priority, deepcopy(result)
        return priority, deepcopy(result)

    def publication(*args, **kwargs):
        # PreparedRadio reads published intensity bands. Model that collaborator
        # directly instead of rescoring the entire fixture on every point read.
        bands = {}
        for mode, rows in pools.items():
            bands[mode] = defaultdict(list)
            for track in rows:
                bands[mode][musical_character.intensity(track)].append(track)

        def tracks(surface, mode, purpose, *, allowed, excluded=(), used=(), limit=LOOKAHEAD):
            return deepcopy([track for track in pools[mode] if track['key'] not in used
                and track['video_id'] not in excluded and allowed(track)][:limit])

        return SimpleNamespace(generation=1, current_reader=None, has_intensity=inputs_state.indexed,
            surface=lambda name: name, positions=lambda *args: {}, close=lambda: None,
            planning_read=nullcontext, planning_fact=lambda key, load: load(),
            track_facts=lambda name, keys, load: load(keys),
            tracks=Mock(spec=PreparedRadio.tracks, side_effect=tracks),
            intensity_pool=Mock(spec=PreparedRadio.intensity_pool,
                side_effect=lambda *args, **kwargs: intensity_pool(bands, *args, **kwargs)))

    def inputs(*args, **kwargs):
        return None, 'controlled', 'controlled', modes, {
            '_radio': {'generation': None, 'space': 'controlled',
                       'members': {mid: len(rows) for mid, rows in pools.items()} if inputs_state.indexed else {}},
            **{mid: {'_radio_pool': deepcopy(rows[:12])} for mid, rows in pools.items()}}

    monkeypatch.setattr('yt_playlist.rec.stored_radio.PreparedRadio', publication)
    monkeypatch.setattr(radio_candidates, '_prepared_inputs', inputs)
    monkeypatch.setattr(musical_character, 'load',
        lambda store, keys: {key: deepcopy(profiles[key]) for key in keys})
    inputs_state.store, inputs_state.pools, inputs_state.quiet = store, pools, quiet
    inputs_state.modes = modes
    inputs_state.tracks, inputs_state.vectors = all_tracks, vectors
    inputs_state.profiles, inputs_state.weights, inputs_state.reads = profiles, weights, reads
    return inputs_state


@pytest.fixture
def catalog_inputs(intensity_inputs):
    """An unindexed published seed with controlled repository catalog pages.

    Keep RadioCandidates and radio_catalog's paging/filtering real. Catalog SQL
    eligibility, aliases, upload overrides and hydration retain their owners in
    test_radio_catalog and test_stored_radio.
    """
    from yt_playlist.repos.radio_catalog import RadioCatalogRepo

    inputs = intensity_inputs
    inputs.indexed = False
    seed = inputs.pools['1'][0]
    seed['genre'] = 'Downtempo'
    mode = inputs.modes['1']
    inputs.modes.clear()
    inputs.modes['1'] = mode
    inputs.pools.clear()
    inputs.pools['1'] = [seed]
    rows = []

    def add(prefix, count, *, artist=None):
        added = []
        for index in range(count):
            key = f'{prefix}-{index}'
            row = {**deepcopy(seed), 'id': len(rows) + 1, 'key': key,
                   'video_id': key, 'title': key, 'artist': artist or key}
            rows.append(row)
            added.append(row)
            inputs.tracks[key] = row
            inputs.profiles[key] = row['musical']
            inputs.vectors[key] = mode['centroid'].tobytes()
        return added

    inputs.store.radio_catalog.genre_labels = Mock(
        spec=RadioCatalogRepo.genre_labels, side_effect=lambda: list({r['genre'] for r in rows}))
    inputs.store.radio_catalog.genre_tracks = Mock(spec=RadioCatalogRepo.genre_tracks,
        side_effect=lambda labels, after, limit: deepcopy([
            r for r in rows if r['id'] > after and r['genre'] in labels][:limit]))
    return SimpleNamespace(store=inputs.store, seed=seed, add=add,
                           pages=inputs.store.radio_catalog.genre_tracks)
