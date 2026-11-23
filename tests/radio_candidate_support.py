"""Controlled prepared inputs for Radio policy checks, without library preparation.

RadioCandidates and RadioDirections remain real. The publication reader, catalog,
profiles and vectors are collaborators; their SQL/file contracts have real owners
in test_stored_radio and test_typed_recommendation_publication.
"""
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from itertools import chain, islice
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from yt_playlist.rec import musical_character, radio_candidates, radio_intensity
from yt_playlist.rec.vibe_score_policy import serving_prefix
from yt_playlist.rec.stored_radio import LOOKAHEAD, PreparedRadio
from yt_playlist.repos.tracks import TrackRepo


@pytest.fixture
def intensity_inputs(monkeypatch):
    inputs_state = SimpleNamespace(indexed=True)
    pools, modes, profiles, vectors = {}, {}, {}, {}
    labels = ['Electronic'] * 5 + ['Synthwave', 'Jazz', 'Rock', 'Ambient']
    stamp = serving_prefix() + 'radio-test'
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
                       'vibes': {'inputs_version': stamp, 'intensity_fits': {
                           'energize': {'score': energy, 'support': 1, 'source': 'audio_measurement'}}}}
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

    def genre_tracks(labels, after, limit):
        # The controlled catalog contains the recordings selected for this
        # scenario. Tests with extra catalog inventory supply it explicitly.
        rows = ({**track, 'id': index} for index, track in enumerate(chain.from_iterable(pools.values()), 1)
                if index > after and track['genre'] in labels)
        return deepcopy(list(islice(rows, limit)))

    store = SimpleNamespace(
        _lock=nullcontext(),
        settings=SimpleNamespace(get_setting=lambda name: {'rec_param:now_gain': '0'}.get(name)),
        rec_model=SimpleNamespace(disliked_identity_keys=set, disliked_key_page=lambda **kwargs: [],
            recording_encoder=lambda: 'controlled', recording_state=lambda: SimpleNamespace(generation=None),
            vectors_by_keys=lambda keys: {}, content_vectors_by_keys=lambda keys: {},
            suppressed_keys=lambda *args: set(),
            muted_artists=list, get_weights=lambda **kwargs: weights,
            content_vector_pools=lambda keys: {'library': {key: vectors[key] for key in keys},
                                               'discovered': {}}),
        genres=SimpleNamespace(feature_axes=axes, all_genres=lambda: labels, get_genre_whitelist=list),
        rec_proposals=SimpleNamespace(get_proposals=lambda surface: None),
        recommendations=SimpleNamespace(ranks=lambda *args, **kwargs: {},
            intensity_bands=lambda *args, **kwargs: [2, 48]),
        rec_query=SimpleNamespace(tracks_by_keys=lambda keys: {}),
        accounts=SimpleNamespace(get_accounts=list),
        tracks=SimpleNamespace(preferred_playback_uploads=Mock(
            spec=TrackRepo.preferred_playback_uploads, return_value={})),
        radio_catalog=SimpleNamespace(details=lambda keys: {key: all_tracks[key] for key in keys},
            genre_labels=lambda: sorted({track['genre'] for track in chain.from_iterable(pools.values())
                                         if track['genre']}),
            genre_tracks=genre_tracks,
            rotation_facts=lambda keys: {key: {'owned': not all_tracks[key].get('_radio_fresh'),
                'heard': bool(all_tracks[key].get('plays')), 'last_started': None, 'last_offered': None}
                for key in keys if key in all_tracks}))

    store.read_view = lambda **kwargs: nullcontext(store)

    def intensity_pool(bands, mode, preference, *, allowed, genre_allowed, excluded, used, limit=LOOKAHEAD,
                       max_priority=None):
        assert 1 <= limit <= LOOKAHEAD
        reads.append((mode, limit, frozenset(used)))
        result, priority = [], None
        ordered = sorted(bands[mode], key=lambda value: radio_intensity.priority(value, preference))
        for value in ordered:
            rank = radio_intensity.priority(value, preference)
            if (priority is not None and rank > priority) or (max_priority is not None and rank > max_priority):
                break
            for track in bands[mode][value]:
                if (track['key'] in used or track['video_id'] in excluded
                        or not genre_allowed(track['genre']) or not allowed(track)):
                    continue
                priority = rank
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
        headings = {}
        for mode, region in bands.items():
            mode_headings = defaultdict(set)
            for value, rows in region.items():
                band = round(value * radio_intensity.RESOLUTION)
                mode_headings[band].update(track['genre'] for track in rows)
            headings[mode] = mode_headings

        def tracks(surface, mode, purpose, *, allowed, excluded=(), used=(), limit=LOOKAHEAD):
            return deepcopy([track for track in pools[mode] if track['key'] not in used
                and track['video_id'] not in excluded and allowed(track)][:limit])

        return SimpleNamespace(generation=1, current_reader=None, has_intensity=inputs_state.indexed,
            surface=lambda name: name, positions=lambda *args: {}, close=lambda: None,
            planning_read=nullcontext, planning_fact=lambda key, load: load(),
            intensity_headings=lambda: headings,
            track_facts=lambda name, keys, load: load(keys),
            tracks=Mock(spec=PreparedRadio.tracks, side_effect=tracks),
            intensity_pool=Mock(spec=PreparedRadio.intensity_pool,
                side_effect=lambda *args, **kwargs: intensity_pool(bands, *args, **kwargs)))

    def inputs(*args, **kwargs):
        if kwargs.get('capture'):
            kwargs['capture'](store)
        return None, 'controlled', 'controlled', modes, {
            '_radio': {'generation': None, 'space': 'controlled',
                       'members': {mid: len(rows) for mid, rows in pools.items()} if inputs_state.indexed else {}},
            **{mid: {'_radio_pool': deepcopy(rows[:12])} for mid, rows in pools.items()}}

    monkeypatch.setattr('yt_playlist.rec.stored_radio.PreparedRadio', publication)
    monkeypatch.setattr(radio_candidates, 'prepared_inputs', inputs)
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
