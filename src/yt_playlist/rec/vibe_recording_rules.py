"""Prepare the three vibe layers off the request path, without annotation reads."""
import json
import hashlib

from yt_playlist.core import audio_vibe_metadata
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS, serving_compatible
from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import audio_vibe_specialists, audio_vibe_prediction, home_vibe, vibe_intensity
from yt_playlist.rec import musical_profiles as profiles
from yt_playlist.rec import vibe_prediction as policy
from yt_playlist.rec import audio_vibe_arc, vibe_joint_model


def serving_prefix():
    """Compatible fit semantics, independent of a replacement model's identity."""
    return '/'.join(('recording-vibes-1', policy.VERSION, profiles.VERSION, audio_vibe_prediction.VERSION)) + '/'


def compatible_version(stamp):
    prefix = serving_prefix()
    return isinstance(stamp, str) and stamp.startswith(prefix) and len(stamp) > len(prefix)


def version():
    return serving_prefix() + '/'.join((audio_vibe_arc.VERSION, vibe_intensity.VERSION,
                                       audio_vibe_specialists.identity()))


def measured(inputs, sources, selected, overrides, *, prediction=None):
    evidence = _measured(inputs, sources, selected, overrides, prediction=prediction)
    if evidence:
        evidence.pop('model', None)
        evidence.setdefault('sample_ids', [])
        raw = profiles.canonical({'samples': inputs['samples'], 'metadata': inputs['metadata'],
                                  'sources': sources, 'selected': selected, 'overrides': overrides})
        evidence['input_digest'] = hashlib.sha256(raw.encode()).hexdigest()
    return evidence


def _measured(inputs, sources, selected, overrides, *, prediction):
    samples = inputs['samples']
    if samples:
        result = prediction.predict(samples, metadata=inputs['metadata'])
        suitability = dict((result or {}).get('suitability') or {})
        suitability.update({channel: suitability[vibe] for channel, vibe in INTENSITY_CHANNELS.items()
                            if vibe in suitability})
        if suitability:
            return {'source': 'audio_measurement', 'sample_ids': sorted(s['id'] for s in samples),
                    'fits': {vibe: {'score': score, 'support': 1.} for vibe, score in suitability.items()}}
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
    for channel, vibe in INTENSITY_CHANNELS.items():
        if vibe in fits:
            fits[channel] = dict(fits[vibe])
    return {'source': 'provider_measurement', 'fits': fits} if fits else None


def recording_values(rows, inputs, source_inputs, axes, predictor, stamp):
    tracks, changed = {}, {}
    decoded = {key: json.loads(raw) for key, _revision, raw, _digest, _different in rows}
    for key, _revision, raw, _digest, _different in rows:
        check_stopping()
        features = decoded[key]
        features = features['library'] or features['discovered']
        if features is None and inputs[key]['samples'] and inputs[key]['metadata']:
            # A played, exactly matched catalog recording can teach the library
            # before the listener saves it. It need not become a recommendation.
            metadata = inputs[key]['metadata']
            features = {'genre': metadata.get('genre'), 'audio': metadata}
        old = inputs[key]['old']
        track = None
        if features is not None:
            sources, overrides, _truncated = source_inputs[key]
            sources = [s for s in sources if s['version'] == profiles.VERSION]
            genre = overrides.get('genre') if 'genre' in overrides else features.get('genre')
            track = {'artist': inputs[key]['artist'], 'genre': audio_vibe_metadata.subgenre({'genre': genre}, genre_axes=axes),
                     'evidence': measured(inputs[key], sources, features.get('audio'), overrides, prediction=predictor), 'version': stamp}
        tracks[key] = track
        if old != track:
            changed[key] = (old, track)
    return tracks, changed


def cohort_keys(tracks, musical):
    usable = {key for key, payload, _version, _digest in musical if payload is not None}
    channels = {*home_vibe.PRESETS, *INTENSITY_CHANNELS}
    # Own measurements win every measured channel. Only tracks with a missing
    # channel consume cohort statistics; changes still fan out to all affected
    # cohorts, including old memberships and groups outside this batch.
    return {group for key in usable
                  if not tracks[key]['evidence'] or channels - tracks[key]['evidence']['fits'].keys()
                  for group in policy.groups(tracks[key]['artist'], tracks[key]['genre'])}


def cohort_values(cohorts, changed):
    touched = set()
    for key, (old, new) in changed.items():
        for track, remove in ((old, True), (new, False)):
            if not track or not track['evidence'] or not serving_compatible(track['version']):
                continue
            for group in policy.groups(track['artist'], track['genre']):
                touched.add(group)
                if group not in cohorts:
                    continue
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
    return touched


def posterior_values(posterior, eligible, recording_ids, tracks, albums, masked, vibe):
    training = {row['recording'] for row in posterior.rows}
    estimates = {}
    for key in eligible:
        check_stopping()
        # A retraction in this batch may precede durable invalidation.
        if recording_ids[key] not in training:
            estimates[key] = posterior.predict(json.loads(tracks[key]['artist'] or '[]'),
                tracks[key]['genre'], album=albums.get(key), recording=recording_ids[key],
                metadata_prior=home_vibe.heuristic_affinity(masked[key], INTENSITY_CHANNELS.get(vibe, vibe))
                    if masked[key] is not None else .5)
    return estimates


def resolved_values(musical, tracks, cohorts, pooled, retained, albums, masked, empty, stamp):
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
        for channel, vibe in INTENSITY_CHANNELS.items():
            fallback[channel] = fallback[vibe]
        candidates = []
        for group in policy.groups(track['artist'], track['genre']):
            stats = cohorts[group]['stats'] if group in cohorts else {}
            if track['evidence']:
                stats = policy.adjust(stats, track['evidence'], -1)
            candidates.append((group, stats))
        profile['vibes'] = policy.resolve(fallback, track['evidence'], candidates)
        for vibe, fit in profile['vibes']['fits'].items():
            if fit['source'] in ('audio_measurement', 'provider_measurement'):
                continue
            prepared = pooled[vibe]
            if prepared:
                generation, estimates = prepared
                estimate = estimates.get(key)
                if estimate:
                    profile['vibes']['fits'][vibe] = {**estimate, 'generation': generation,
                        'model_version': vibe_joint_model.VERSION,
                        'conditioning': {'genre': track['genre'], 'album': albums.get(key),
                            'metadata_prior': home_vibe.heuristic_affinity(masked[key], INTENSITY_CHANNELS.get(vibe, vibe))
                                if masked[key] is not None else .5}}
            elif vibe in retained.get(key, {}) and vibe not in empty:
                profile['vibes']['fits'][vibe] = retained[key][vibe]
        profile['vibes']['inputs_version'] = stamp
        resolved[key] = {}
        for vibe, fit in profile['vibes']['fits'].items():
            projected = dict(fit)
            if fit['source'] in policy.CAPS and not fit.get('generation') and not fit.get('retain'):
                group = next(group for group, _stats in candidates if json.loads(group)[0] == fit['source'])
                projected['inputs'] = {identity: mid for identity, mid in cohorts[group]['inputs'].get(vibe, {}).items()
                                       if identity != key}
            resolved[key][vibe] = projected
        profile['vibes']['intensity_fits'] = {
            vibe: profile['vibes']['fits'].pop(channel) for channel, vibe in INTENSITY_CHANNELS.items()
            if channel in profile['vibes']['fits']}
        updated.append((key, profiles.canonical(profile), profile_version, profiles.digest(profile)))
    return updated, resolved
