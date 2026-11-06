"""Prepare the three vibe layers off the request path, without annotation reads."""
import json
import hashlib

from yt_playlist.core import audio_vibe_metadata, fao
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.core.activity import check_stopping, WorkBudgetExceeded
from yt_playlist.rec import audio_vibe_cruise, audio_vibe_energize, audio_vibe_focus, audio_vibe_model, audio_vibe_prediction, audio_vibe_relax, audio_vibe_release, audio_vibe_uplift, home_vibe
from yt_playlist.rec import musical_profiles as profiles
from yt_playlist.rec import vibe_prediction as policy
from yt_playlist.rec import vibe_joint_model, vibe_joint_preparation


def serving_prefix():
    """Compatible fit semantics, independent of a replacement model's identity."""
    return '/'.join(('recording-vibes-1', policy.VERSION, profiles.VERSION, audio_vibe_prediction.VERSION)) + '/'


def compatible_version(stamp):
    prefix = serving_prefix()
    return isinstance(stamp, str) and stamp.startswith(prefix) and len(stamp) > len(prefix)


def version():
    return serving_prefix() + '/'.join((
                     audio_vibe_cruise.identity(), audio_vibe_release.identity(),
                     audio_vibe_relax.identity(), audio_vibe_uplift.identity(),
                     audio_vibe_energize.identity(), audio_vibe_focus.identity(), audio_vibe_model.identity()))


def due(store):
    return (store.settings.get_setting('rec_vibe_version') != version()
            or store.vibe_predictions.pending() is not None or vibe_joint_preparation.due(store))


def step(store, batch=32, *, seconds=10, memory_bytes=vibe_joint_model.MEMORY_BYTES):
    store.vibe_predictions.ensure_version(version())
    waiting = store.prepared_content.has_pending() or store.musical_profiles.pending()
    if vibe_joint_preparation.admitted(store, waiting=waiting):
        if vibe_joint_preparation.step(store, seconds=seconds, memory_bytes=memory_bytes):
            return True
    if store.vibe_predictions.fanout(batch):
        return True
    return False


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
        prediction = audio_vibe_prediction.predict(samples, metadata=inputs['metadata'])
        shared_scores = {row['vibe']: row['score'] for row in prediction['ranking']}
        suitability = {**(prediction.get('suitability') or shared_scores),
                       **{channel: shared_scores[vibe] for channel, vibe in INTENSITY_CHANNELS.items()}}
        return {'source': 'audio_measurement', 'sample_ids': sorted(s['id'] for s in samples),
                'model': prediction.get('model'), 'fits': {
                    vibe: {'score': score, 'support': 1.} for vibe, score in suitability.items()}}
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


def recordings(reader, rows, musical, *, source_inputs=None):
    inputs = reader.vibe_predictions.inputs([row[0] for row in rows])
    recording_ids = {key:value['recording'] for key,value in inputs.items()}
    if source_inputs is None:
        source_inputs = reader.musical_profiles.recording_inputs([row[0] for row in rows])
    tracks, changed = {}, {}
    stamp = version()
    decoded = {key: json.loads(raw) for key, _revision, raw, _digest, _different in rows}
    labels = {features.get('genre') for payload in decoded.values()
              for features in (payload.get('library'), payload.get('discovered')) if features is not None}
    labels.update(value['metadata'].get('genre') for value in inputs.values() if value.get('metadata'))
    labels.update(overrides.get('genre') for _sources, overrides, _truncated in source_inputs.values())
    axes = reader.genres.feature_axes(labels)
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
                     'evidence': measured(inputs[key], sources, features.get('audio'), overrides), 'version': stamp}
        tracks[key] = track
        if old != track:
            changed[key] = (old, track)
    usable = {key for key, payload, _version, _digest in musical if payload is not None}
    channels = {*home_vibe.PRESETS, *INTENSITY_CHANNELS}
    # Own measurements win every measured channel. Only tracks with a missing
    # channel consume cohort statistics; changes still fan out to all affected
    # cohorts, including old memberships and groups outside this batch.
    group_keys = {group for key in usable
                  if not tracks[key]['evidence'] or channels - tracks[key]['evidence']['fits'].keys()
                  for group in policy.groups(tracks[key]['artist'], tracks[key]['genre'])}
    cohorts = reader.vibe_predictions.groups(sorted(group_keys))
    touched = set()
    for key, (old, new) in changed.items():
        for track, remove in ((old, True), (new, False)):
            if not track or not track['evidence']:
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
    updated, resolved = [], {}
    retained = reader.vibe_predictions.retained_joint_fits(list(tracks))
    albums = reader.vibe_predictions.joint_albums(list(tracks)) if vibe_joint_preparation.qualified() else {}
    pooled, repairs = {}, set()
    for vibe in (*home_vibe.PRESETS, *INTENSITY_CHANNELS):
        eligible = [key for key in usable if not tracks[key]['evidence']
                    or vibe not in tracks[key]['evidence']['fits']]
        pooled[vibe] = None
        if not eligible:
            continue
        try:
            prepared = vibe_joint_preparation.posterior(reader, vibe)
            if prepared:
                generation, posterior = prepared
                training = {row['recording'] for row in posterior.rows}
                estimates = {}
                for key in eligible:
                    check_stopping()
                    # A same-batch retraction can precede SQL invalidation.
                    # Never predict from the target's own previous measurement.
                    if recording_ids[key] not in training:
                        estimates[key] = posterior.predict(json.loads(tracks[key]['artist'] or '[]'),
                            tracks[key]['genre'], album=albums.get(key), recording=recording_ids[key])
                pooled[vibe] = generation, estimates
                # Retain only bounded score receipts across channels, never
                # eight dense posteriors during a foreground preparation turn.
                del posterior
            del prepared
        except fao.CorruptFile:
            repairs.add('!joint:' + vibe)
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
                        'conditioning': {'genre': track['genre'], 'album': albums.get(key)}}
            elif (vibe in retained.get(key, {}) and not (
                    vibe_joint_preparation.current(reader, state := vibe_joint_preparation.state(reader, vibe))
                    and state.get('outcome') == 'empty')):
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
            vibe: profile['vibes']['fits'].pop(channel) for channel, vibe in INTENSITY_CHANNELS.items()}
        updated.append((key, profiles.canonical(profile), profile_version, profiles.digest(profile)))
    if version() != stamp:
        raise WorkBudgetExceeded('Configured vibe model changed during capture')
    return updated, (tracks, resolved, sorted(touched | repairs))
