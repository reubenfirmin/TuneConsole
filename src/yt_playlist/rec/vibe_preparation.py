"""Prepare the three vibe layers off the request path, without annotation reads."""
import json
import hashlib

from yt_playlist.core import audio_vibe_metadata
from yt_playlist.core.activity import check_stopping, WorkBudgetExceeded
from yt_playlist.rec import audio_vibe_model, audio_vibe_preview, home_vibe
from yt_playlist.rec import musical_profiles as profiles
from yt_playlist.rec import vibe_prediction as policy


def version():
    return '/'.join(('recording-vibes-1', policy.VERSION, profiles.VERSION, audio_vibe_preview.VERSION, audio_vibe_model.identity()))


def due(store):
    return (store.settings.get_setting('rec_vibe_version') != version()
            or store.vibe_predictions.pending() is not None)


def step(store, batch=32):
    store.vibe_predictions.ensure_version(version())
    return store.vibe_predictions.fanout(batch)


def measured(inputs, sources, selected, overrides):
    evidence = _measured(inputs, sources, selected, overrides)
    if evidence:
        evidence.pop('model', None)
        evidence.setdefault('sample_ids', [])
        raw = profiles.canonical({'samples': inputs['samples'], 'metadata': inputs['metadata'],
                                  'sources': sources, 'selected': selected, 'overrides': overrides})
        evidence['input_digest'] = hashlib.sha256(raw.encode()).hexdigest()
    return evidence


def _measured(inputs, sources, selected, overrides):
    samples = inputs['samples']
    if samples:
        prediction = audio_vibe_preview.predict(samples, metadata=inputs['metadata'])
        return {'source': 'audio_measurement', 'sample_ids': sorted(s['id'] for s in samples),
                'model': prediction.get('model'), 'fits': {
                    row['vibe']: {'score': row['score'], 'support': 1.} for row in prediction['ranking']}}
    # Provider classifiers (AcousticBrainz et al) and selected numeric catalog
    # attributes are own evidence. Provider tags, artist/release descriptions,
    # curated genre priors and propagated predictions cannot seed new cohorts.
    native = []
    for source in sources:
        if source['scope'] != 'recording':
            continue
        observations = {name: value for name, value in source['observations'].items()
                        if name.startswith('mood_') or name in ('bpm', 'instrumental', 'danceability')}
        if observations:
            native.append(profiles.source_profile({**source, 'observations': observations}))
    fields = {'energy', 'bpm', 'instrumental', 'danceability', 'mood_relaxed', 'mood_acoustic',
              'mood_happy', 'mood_sad', 'mood_aggressive', 'mood_party'}
    own = profiles.resolve(native, selected={k: v for k, v in (selected or {}).items() if k in fields},
                           overrides={k: v for k, v in overrides.items() if k in fields})
    # A lone legacy derived energy value remains the existing fallback, rather
    # than manufacturing a new measurement cohort. Retain it alongside real
    # mood/instrumental evidence: partial provider data must not erase known energy.
    if not any(own['dimensions'][axis]['support'] for axis in
               ('instrumental', 'relaxed', 'happiness', 'sadness', 'aggression')) and not any(
            'mood_party' in source['observations'] for source in native):
        return None
    fits = {}
    for vibe, preset in home_vibe.PRESETS.items():
        support = sum(abs(weight) * own['dimensions'][axis]['support']
                      for axis, weight in preset['weights'].items()) / sum(abs(w) for w in preset['weights'].values())
        if support > 0:
            fits[vibe] = {'score': home_vibe.heuristic_affinity(own, vibe), 'support': support}
    return {'source': 'provider_measurement', 'fits': fits} if fits else None


def recordings(reader, rows, musical, *, source_inputs=None):
    inputs = reader.vibe_predictions.inputs([row[0] for row in rows])
    if source_inputs is None:
        source_inputs = reader.musical_profiles.recording_inputs([row[0] for row in rows])
    tracks, changed, group_keys = {}, {}, set()
    stamp = version()
    for key, _revision, raw, _digest, _different in rows:
        check_stopping()
        features = json.loads(raw)
        features = features['library'] or features['discovered']
        if features is None and inputs[key]['samples'] and inputs[key]['metadata']:
            # A played, exactly matched catalog recording can teach the library
            # before the listener saves it. It need not become a recommendation.
            metadata = inputs[key]['metadata']
            features = {'genre': metadata.get('genre'), 'audio': metadata}
        old = inputs[key]['old']
        if old:
            group_keys.update(policy.groups(old['artist'], old['genre']))
        track = None
        if features is not None:
            sources, overrides, _truncated = source_inputs[key]
            sources = [s for s in sources if s['version'] == profiles.VERSION]
            genre = overrides.get('genre') if 'genre' in overrides else features.get('genre')
            track = {'artist': inputs[key]['artist'], 'genre': audio_vibe_metadata.subgenre({'genre': genre}),
                     'evidence': measured(inputs[key], sources, features.get('audio'), overrides), 'version': stamp}
            group_keys.update(policy.groups(track['artist'], track['genre']))
        tracks[key] = track
        if old != track:
            changed[key] = (old, track)
    cohorts = reader.vibe_predictions.groups(sorted(group_keys))
    touched = set()
    for key, (old, new) in changed.items():
        for track, remove in ((old, True), (new, False)):
            if not track or not track['evidence']:
                continue
            for group in policy.groups(track['artist'], track['genre']):
                cohort = cohorts[group]
                cohort['stats'] = policy.adjust(cohort['stats'], track['evidence'], -1 if remove else 1)
                for vibe, fit in track['evidence']['fits'].items():
                    if fit['support'] <= 0:
                        continue
                    inputs = cohort['inputs'].setdefault(vibe, {})
                    if remove:
                        inputs.pop(key, None)
                    else:
                        inputs[key] = None  # Resolved to the new immutable measurement at publication.
                touched.add(group)
    updated, resolved = [], {}
    for key, payload, profile_version, _digest in musical:
        if payload is None:
            updated.append((key, payload, profile_version, None))
            track = tracks[key]
            if track and track['evidence']:
                resolved[key] = {vibe: {**fit, 'source': track['evidence']['source']}
                                 for vibe, fit in track['evidence']['fits'].items()}
            continue
        profile, track = json.loads(payload), tracks[key]
        fallback = {vibe: home_vibe.heuristic_affinity(profile, vibe) for vibe in home_vibe.PRESETS}
        candidates = []
        for group in policy.groups(track['artist'], track['genre']):
            stats = cohorts[group]['stats']
            if track['evidence']:
                stats = policy.adjust(stats, track['evidence'], -1)
            candidates.append((group, stats))
        profile['vibes'] = policy.resolve(fallback, track['evidence'], candidates)
        profile['vibes']['inputs_version'] = stamp
        resolved[key] = {}
        for vibe, fit in profile['vibes']['fits'].items():
            projected = dict(fit)
            if fit['source'] in policy.CAPS:
                group = next(group for group, _stats in candidates if json.loads(group)[0] == fit['source'])
                projected['inputs'] = {identity: mid for identity, mid in cohorts[group]['inputs'].get(vibe, {}).items()
                                       if identity != key}
            resolved[key][vibe] = projected
        updated.append((key, profiles.canonical(profile), profile_version, profiles.digest(profile)))
    if version() != stamp:
        raise WorkBudgetExceeded('Configured vibe model changed during capture')
    return updated, (tracks, resolved, sorted(touched))
