"""Prepare the three vibe layers off the request path, without annotation reads."""
import json
import hashlib

from yt_playlist.core import audio_vibe_metadata
from yt_playlist.core.audio_vibe_contract import EXCERPT_SUPPORT_POLICY, INTENSITY_CHANNELS, JOINT_AUDIO_POLICY, PREDICTION_VERSION
from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import acousticbrainz_vibes, audio_vibe_specialists, vibe_intensity, vibe_presets, vibe_score_versions
from yt_playlist.rec import musical_profiles as profiles
from yt_playlist.rec import audio_vibe_arc
from yt_playlist.rec.vibe_score_versions import compatible_version, serving_prefix


def version():
    return serving_prefix() + '/'.join((audio_vibe_arc.VERSION, vibe_intensity.VERSION,
                                       audio_vibe_specialists.identity()))


def joint_policy():
    return version() + '/' + JOINT_AUDIO_POLICY


def rebuild_version():
    return '/'.join((version(), EXCERPT_SUPPORT_POLICY, 'compatible-capture-selection-1',
                     vibe_score_versions.joint_prediction_policy(), acousticbrainz_vibes.version_prefix()))


def measured(inputs, sources, overrides, *, prediction=None):
    evidence = _measured(inputs, sources, overrides, prediction=prediction)
    if evidence:
        evidence.pop('model', None)
        evidence.setdefault('sample_ids', [])
        raw = profiles.canonical({'samples': inputs['samples'], 'metadata': inputs['metadata'],
                                  'sources': sources, 'overrides': overrides})
        evidence['input_digest'] = hashlib.sha256(raw.encode()).hexdigest()
    return evidence


def _measured(inputs, sources, overrides, *, prediction):
    samples = inputs['samples']
    if samples:
        result = prediction.predict(samples, metadata=inputs['metadata'], require_complete=False)
        suitability = dict((result or {}).get('suitability') or {})
        suitability.update({channel: suitability[vibe] for channel, vibe in INTENSITY_CHANNELS.items()
                            if vibe in suitability})
        if suitability:
            support = len(samples) / 3
            return {'source': 'audio_measurement', 'sample_ids': sorted(s['id'] for s in samples),
                    'fits': {vibe: {'score': score, 'support': support} for vibe, score in suitability.items()}}
    return None


def recording_values(rows, inputs, source_inputs, axes, predictor, stamp, *, matches, model):
    tracks = {}
    estimate_version = acousticbrainz_vibes.estimate_version(model) if model is not None else None
    for key, _revision, raw, _digest, _different in rows:
        check_stopping()
        features = json.loads(raw)
        features = features['library'] or features['discovered']
        if features is None and inputs[key]['samples'] and inputs[key]['metadata']:
            # A played, exactly matched catalog recording can teach the library
            # before the listener saves it. It need not become a recommendation.
            metadata = inputs[key]['metadata']
            features = {'genre': metadata.get('genre'), 'audio': metadata}
        track = None
        if features is not None:
            native, overrides, _truncated = source_inputs[key]
            sources = [s for s in native if s['version'] == profiles.VERSION]
            genre = overrides.get('genre') if 'genre' in overrides else features.get('genre')
            evidence = measured(inputs[key], sources, overrides, prediction=predictor)
            track = {'artist': inputs[key]['artist'], 'genre': audio_vibe_metadata.subgenre({'genre': genre}, genre_axes=axes),
                     'evidence': evidence, 'version': stamp}
            if key in matches:
                source_key, mbid = matches[key]
                classifiers = acousticbrainz_vibes.input_values(native, mbid)
                if classifiers is not None:
                    track['acousticbrainz_input'] = (source_key, mbid, profiles.canonical(classifiers))
                    measured_fits = evidence['fits'] if evidence else {}
                    missing = {vibe: row for vibe, row in (model or {}).items() if vibe not in measured_fits}
                    if missing:
                        track['acousticbrainz'] = {
                            vibe: {'score': score, 'support': 1., 'source': acousticbrainz_vibes.SOURCE,
                                   'inputs_version': estimate_version}
                            for vibe, score in acousticbrainz_vibes.score(classifiers, missing).items()}
        tracks[key] = track
    return tracks


def posterior_values(posterior, eligible, recording_ids, tracks, albums, masked, vibe):
    credits = {key: json.loads(tracks[key]['artist'] or '[]') for key in eligible}
    sampled_genres = {artist: set() for artists in credits.values() for artist in artists}
    if not sampled_genres:
        return {}

    training = set()
    for row in posterior.rows:
        check_stopping()
        training.add(row['recording'])
        if row['source'].startswith('audio_measurement/'):
            for artist in row['artists']:
                if artist in sampled_genres:
                    sampled_genres[artist].add(row['genre'])
    estimates = {}
    for key, artists in credits.items():
        check_stopping()
        # A retraction in this batch may precede durable invalidation.
        if recording_ids[key] in training:
            continue
        related = [sampled_genres[artist] for artist in artists if sampled_genres[artist]]
        if not related:
            continue
        genre = tracks[key]['genre']
        source = 'artist_genre' if genre and any(genre in genres for genres in related) else 'artist'
        estimate = posterior.predict(artists, genre, album=albums.get(key), recording=recording_ids[key],
            metadata_prior=vibe_presets.heuristic_affinity(masked[key], INTENSITY_CHANNELS.get(vibe, vibe))
                if masked[key] is not None else .5)
        if estimate:
            estimates[key] = {**estimate, 'source': source}
    return estimates


def conditioning(track, album, masked_profile, vibe):
    prior = vibe_presets.heuristic_affinity(masked_profile, INTENSITY_CHANNELS.get(vibe, vibe)) if masked_profile is not None else .5
    return {'genre': track['genre'], 'album': album, 'metadata_prior': prior}


def resolved_values(musical, tracks, pooled, retained, albums, masked, empty, stamp, joint_stamp,
                    retained_conditioning):
    updated, resolved = [], {}
    for key, payload, profile_version, _digest in musical:
        track = tracks[key]
        fits = dict(track.get('acousticbrainz', {})) if track else {}
        evidence = track['evidence'] if track else None
        if evidence:
            fits.update({vibe: {**fit, 'source': evidence['source']} for vibe, fit in evidence['fits'].items()})
        if payload is None:
            updated.append((key, payload, profile_version, None))
            resolved[key] = fits
            continue
        profile = json.loads(payload)
        profile['vibes'] = {'version': PREDICTION_VERSION, 'fits': fits}
        for vibe, prepared in pooled.items():
            if vibe in fits or track is None:
                continue
            if prepared:
                generation, estimates = prepared
                estimate = estimates.get(key)
                if estimate:
                    profile['vibes']['fits'][vibe] = {**estimate, 'generation': generation,
                        'model_version': joint_stamp, 'inputs_version': stamp + '/' + joint_stamp,
                        'conditioning': conditioning(track, albums.get(key), masked.get(key), vibe)}
            elif vibe in retained.get(key, {}) and vibe not in empty:
                old = retained[key][vibe]
                if (old['artist_key'] == track['artist'] and old['subgenre_key'] == track['genre']
                        and retained_conditioning(old['prediction_id'])
                        == conditioning(track, albums.get(key), masked.get(key), vibe)):
                    profile['vibes']['fits'][vibe] = {**{name: old[name] for name in
                        ('score', 'support', 'source', 'recordings', 'variance', 'inputs_version')}, 'retain': True}
        profile['vibes']['inputs_version'] = stamp
        resolved[key] = dict(profile['vibes']['fits'])
        profile['vibes']['intensity_fits'] = {
            vibe: profile['vibes']['fits'].pop(channel) for channel, vibe in INTENSITY_CHANNELS.items()
            if channel in profile['vibes']['fits']}
        updated.append((key, profiles.canonical(profile), profile_version, profiles.digest(profile)))
    return updated, resolved
