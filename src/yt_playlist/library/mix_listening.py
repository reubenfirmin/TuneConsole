"""Prepare the listening adjustment shared by Mix Tape ranks and group selection."""
from dataclasses import dataclass, field
import hashlib
import json

import numpy as np

from yt_playlist.controllers.listening_layers import ListeningLayers
from yt_playlist.core.work_diagnostics import maintenance_job
from yt_playlist.library.listening_composition import layer_plays
from yt_playlist.library.listening_window import listening_window
from yt_playlist.rec import embedding_vectors, group_viz, layers, rec_params, taste_modes


SURFACE = 'mix_listening'
PHASE = 'prepare Mix Tape listening'
VERSION = 2
PARAMETERS = ('now_min_events', 'now_gain', 'session_halflife_h', 'session_alpha')


def _parameters(store):
    return [rec_params.get_param(store, name) for name in PARAMETERS]


def _basis(store):
    state = store.rec_model.recording_state()
    return {
        'collaborative': [state.fit_fingerprint, *(store.rec_model.collaborative_basis() or ())],
        'content': [state.generation, *(store.embeddings.head('rec_content_vectors') or ()),
                    *(store.embeddings.head('rec_discovered_content_vectors') or ())],
    }


@dataclass(frozen=True)
class MixListening:
    """A bounded, detached reading in each space used by the current ranks."""

    at: float = 0.
    now_gain: float = 0.
    today_gain: float = 0.
    half_life: float = 1.
    spaces: dict = field(default_factory=dict)

    def direction(self, space, now, dimension):
        current, today = self.spaces.get(space, (None, None))
        current = np.asarray(current) * self.now_gain if current is not None and len(current) == dimension else None
        if today is None or len(today) != dimension:
            return current
        decayed = np.asarray(today) * self.today_gain * 2 ** ((self.at - now) / (3600 * self.half_life))
        return decayed if current is None else current + decayed


def _capture(store, now, checkpoint):
    window = listening_window(store, now)
    plays = layer_plays(store, now)
    keys = plays.current.keys() | plays.today.keys()
    params = _parameters(store)
    minimum, now_gain, half_life, today_gain = params
    parameters = dict(zip(PARAMETERS, params, strict=True))
    reading = ListeningLayers(parameter=parameters.__getitem__, evidence=None, plays=lambda _: plays)
    generation = store.taste_groups.head_generation()
    spaces = {}
    mode_prior = ({}, 0.)
    for space, rows in (
        ('collaborative', store.rec_model.vectors_by_keys(keys)),
        ('content', store.rec_model.content_vectors_by_keys(keys)),
    ):
        _, matrix, index = embedding_vectors.load_vector_rows(list(rows.items()))
        if matrix is not None:
            tilts = reading.track_tilts(now, matrix, index)
            if any(tilt is not None for tilt in tilts):
                spaces[space] = [tilt.tolist() if tilt is not None else None for tilt in tilts]
            if space == 'content' and generation is None:
                modes = taste_modes.live_modes(store)
                if modes:
                    evidence = layers.RecentEvidence({}, modes, matrix, index, plays)
                    current, _, _ = reading.now_mode_mix(now, evidence=evidence)
                    today, _, _ = reading.session_mode_mix(now, evidence=evidence)
                    mode_prior = (current, now_gain) if current and now_gain else (today or {}, today_gain)
    members = store.taste_groups.leaf_members(generation, keys=keys) if generation and keys else {}
    current, _, _ = group_viz.reading(now, plays.current, members, minimum)
    today, _, _ = group_viz.reading(now, plays.today, members, minimum, half=half_life)
    group_weights, group_gain = (current, now_gain) if current and now_gain else (today, today_gain)
    play_signature = hashlib.sha256(json.dumps([plays.current, plays.today], sort_keys=True).encode()).hexdigest()
    return {'checkpoint': checkpoint, 'at': now, 'expires_at': window.expires_at, 'play_signature': play_signature,
            'basis': _basis(store), 'parameters': params, 'spaces': spaces,
            'group_generation': generation, 'group_weights': group_weights, 'group_gain': group_gain,
            'mode_prior': mode_prior}


def _valid(value, now):
    return value is not None and value['at'] <= now < value['expires_at']


def prepare(store, now):
    """Refresh after source changes or the next local midnight or session expiry."""
    with maintenance_job(PHASE), store.read_snapshot() as snapshot:
        reader = snapshot.store
        previous = getattr(reader, '_prepared_mix_listening', None)
        checkpoint = previous['checkpoint'] if _valid(previous, now) else now
        result = reader.memoized_read((SURFACE, VERSION, checkpoint),
            lambda: _capture(reader, now, checkpoint), owner=SURFACE)
        if result == previous:
            return False
        with store._lock:
            if getattr(store, '_prepared_mix_listening', None) is not previous or not snapshot.is_current():
                return False
            store._prepared_mix_listening = result
            store.maintenance_diagnostics.record('completed', phase=PHASE)
        return True


def _read(store, now):
    value = getattr(store, '_prepared_mix_listening', None)
    if not _valid(value, now) or value['parameters'] != _parameters(store):
        return {}
    return value


def read(store, now):
    value = _read(store, now)
    if not value or not value['spaces']:
        return MixListening()
    _, now_gain, half_life, today_gain = value['parameters']
    basis = _basis(store)
    spaces = {name: directions for name, directions in value['spaces'].items()
              if value['basis'].get(name) == basis[name]}
    return MixListening(value['at'], now_gain, today_gain, half_life, spaces)


def rank_input(store, now):
    value = _read(store, now)
    return value['play_signature'] if value and value['spaces'] else None


def ranking_policy(reading, now):
    """Identify the compatible directions as applied at ranking time."""
    directions = {}
    for space, (current, today) in reading.spaces.items():
        size = len(current if current is not None else today)
        direction = reading.direction(space, now, size)
        directions[space] = hashlib.sha256(np.asarray(direction, dtype='<f8').tobytes()).hexdigest()
    return {'version': VERSION, 'horizons': ['NOW', 'TODAY'], 'directions': directions}


def group_prior(store, head, now):
    value = _read(store, now)
    if value.get('group_generation') != head['generation']:
        return {}, 0.
    weights = value.get('group_weights', {})
    return {group['group_id']: weights[group['version']] for group in head['groups']
            if group['version'] in weights}, value.get('group_gain', 0.)


def mode_prior(store, now):
    value = _read(store, now)
    if value and value['mode_prior'][0] and value['basis']['content'] == _basis(store)['content']:
        return value['mode_prior']
    return {}, 0.
