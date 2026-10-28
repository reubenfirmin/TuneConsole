"""AcousticBrainz enrichment: BPM, a derived energy, and danceability, keyed by MusicBrainz
recording MBID.

AcousticBrainz is a frozen (2022) but still-served CC0 dataset of Essentia acoustic descriptors. It
has no name-based lookup (every query is by MusicBrainz *recording* MBID) so for a track without a
stored MBID we resolve one live via the MusicBrainz provider, then query two endpoints:
low-level (rhythm.bpm) and high-level (mood/danceability classifiers). AcousticBrainz has no native
"energy" scalar, so we derive one from its mood models (see derive_energy). A 404 means "no data for
this MBID" (common, given frozen coverage): a clean miss, not an outage. Failures degrade to Nones.
"""
import logging
import math
import urllib.error

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.integrations.acousticbrainz import AcousticBrainzIAO, API
from yt_playlist.integrations.base import IntegrationUnavailable
from yt_playlist.util import net
from yt_playlist.providers import musicbrainz
from yt_playlist.providers.base import EnrichmentResult, MetadataObservation

logger = logging.getLogger(__name__)

name = "acousticbrainz"
manages_availability = True
_iao = AcousticBrainzIAO()


def access(store=None):
    return _iao.context(store)


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return not _iao.availability().ready(_iao.now())


def reset() -> None:
    """Batch starts do not discard shared cooldowns or useful responses."""


def probe(track, store=None, enrich_fn=None, mbid_fn=None) -> EnrichmentResult:
    """Retain source predictions separately from the legacy typed feature projection."""
    mbid_fn = mbid_fn or (lambda title, artist: musicbrainz.recording_mbid(title, artist, store=store))
    mbid = track.get("mb_recording_id") or mbid_fn(track["title"], track["artist"])
    if not mbid:
        return EnrichmentResult("acousticbrainz", {})
    if enrich_fn is None:
        with access(store):
            low, high = _read(mbid)
        feat = _features(low, high)
    else:
        feat, low, high = enrich_fn(mbid), {}, {}
    fields = {"mb_recording_id": mbid}
    fields.update({k: v for k, v in feat.items()
                   if v is not None and k not in {'mood_party', 'mood_aggressive'}})
    match = {'method': 'supplied_recording_mbid' if track.get('mb_recording_id') else 'musicbrainz_lookup',
             'recording_mbid': mbid}
    observations = []
    for name, (endpoint, path, kind, unit) in _OBSERVATIONS.items():
        if feat.get(name) is None:
            continue
        response = low if endpoint == 'low-level' else high
        model = _object(_object(high.get('highlevel')).get(path.split('.')[1])) if endpoint == 'high-level' else {}
        observations.append(MetadataObservation(
            field=name, value=feat[name], scope='recording', entity_id=mbid, kind=kind, unit=unit,
            source_path=f'{endpoint}:{path}', provider_version=_object(response.get('metadata')).get('version'),
            model_version=model.get('version'), match_evidence=match))
    if feat.get('energy') is not None:
        observations.append(MetadataObservation(
            field='energy', value=feat['energy'], scope='recording', entity_id=mbid, kind='derived',
            unit='legacy_energy_0_1', provider_version=_object(high.get('metadata')).get('version'),
            derivation_version='acousticbrainz-energy-v1',
            dependencies=('mood_party', 'mood_aggressive', 'danceability'),
            match_evidence=match))
    return EnrichmentResult("acousticbrainz", fields, tuple(observations))

def _get_json(url):
    return _iao.get_url(url)


def _prob(highlevel, model, label):
    try:
        raw = highlevel[model]["all"][label]
        value = float(raw)
        return value if not isinstance(raw, bool) and math.isfinite(value) and 0 <= value <= 1 else None
    except (KeyError, TypeError, ValueError):
        return None


# Empirical blend (no external source) mapping AcousticBrainz mood models to a perceived-energy
# scalar: party drives the bulk of it, aggression adds intensity, danceability a smaller lift.
_ENERGY_W_PARTY = 0.5
_ENERGY_W_AGGRESSIVE = 0.3
_ENERGY_W_DANCEABLE = 0.2


def derive_energy(highlevel):
    """Heuristic energy in [0,1] from AcousticBrainz mood classifiers (no native energy field):
    a weighted blend of party/aggressive/danceable (see _ENERGY_W_*).
    Missing components remain unknown; they are not negative classifier results.
    Retained as a legacy derived field until both consumers use shared intensity.
    """
    party = _prob(highlevel, "mood_party", "party")
    aggressive = _prob(highlevel, "mood_aggressive", "aggressive")
    danceable = _prob(highlevel, "danceability", "danceable")
    if any(value is None for value in (party, aggressive, danceable)):
        return None
    return round(_ENERGY_W_PARTY * party + _ENERGY_W_AGGRESSIVE * aggressive
                 + _ENERGY_W_DANCEABLE * danceable, 3)


_FIELDS = ("bpm", "energy", "danceability", "mood_party", "mood_aggressive", "music_key", "music_scale", "mood_happy",
           "mood_sad", "mood_relaxed", "mood_acoustic", "instrumental", "loudness",
           "dynamic_complexity")

# Keep source descriptor units distinct from normalized profile dimensions
# and other providers' gain measurements.
_OBSERVATIONS = {
    'bpm': ('low-level', 'rhythm.bpm', 'prediction', 'bpm'),
    'music_key': ('low-level', 'tonal.key_key', 'prediction', None),
    'music_scale': ('low-level', 'tonal.key_scale', 'prediction', None),
    'loudness': ('low-level', 'lowlevel.average_loudness', 'measurement', 'essentia.average_loudness'),
    'dynamic_complexity': ('low-level', 'lowlevel.dynamic_complexity', 'measurement', 'essentia.dynamic_complexity'),
    **{field: ('high-level', f'highlevel.{model}.all.{label}', 'prediction', 'classifier_probability')
       for field, model, label in (
           ('danceability', 'danceability', 'danceable'),
           ('mood_party', 'mood_party', 'party'),
           ('mood_aggressive', 'mood_aggressive', 'aggressive'),
           ('mood_happy', 'mood_happy', 'happy'),
           ('mood_sad', 'mood_sad', 'sad'),
           ('mood_relaxed', 'mood_relaxed', 'relaxed'),
           ('mood_acoustic', 'mood_acoustic', 'acoustic'),
           ('instrumental', 'voice_instrumental', 'instrumental'))},
}


def _empty():
    return {k: None for k in _FIELDS}


def _object(value):
    return value if isinstance(value, dict) else {}


def _num(d, key):
    v = _object(d).get(key)
    try:
        value = float(v) if v is not None else None
        return value if not isinstance(v, bool) and value is not None and math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _read(mbid):
    """Read endpoints independently so partial failures retain the successful half."""
    low, high = {}, {}
    if not mbid:
        return low, high
    try:
        low = _read_endpoint(mbid, 'low-level')
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001 - sparse frozen dataset: misses are routine
        _record_failure(e)
        logger.debug("AcousticBrainz low-level miss for %s: %s", mbid, e)
    try:
        high = _read_endpoint(mbid, 'high-level')
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception as e:  # noqa: BLE001 - sparse frozen dataset: misses are routine
        _record_failure(e)
        logger.debug("AcousticBrainz high-level miss for %s: %s", mbid, e)
    return low, high


def _record_failure(error):
    if (not isinstance(error, IntegrationUnavailable)
            and not (isinstance(error, urllib.error.HTTPError) and error.code == 404)):
        net.record_probe_failure(error)


def _read_endpoint(mbid, endpoint):
    data = _get_json(f'{API}/{mbid}/{endpoint}')
    if not isinstance(data, dict):
        error = ValueError(f'AcousticBrainz {endpoint} response is not an object')
        raise error
    return data


def _features(low, high):
    feat = _empty()
    feat['energy'] = derive_energy(high.get('highlevel', {}))
    for name, (endpoint, path, _kind, _unit) in _OBSERVATIONS.items():
        value = low if endpoint == 'low-level' else high
        parts = path.split('.')
        for part in parts[:-1]:
            value = _object(value).get(part, {})
        if endpoint == 'high-level':
            model, label = parts[1], parts[-1]
            feat[name] = _prob(high.get('highlevel', {}), model, label)
        elif name in {'music_key', 'music_scale'}:
            text = _object(value).get(parts[-1])
            feat[name] = text if isinstance(text, str) and text.strip() else None
        else:
            feat[name] = _num(value, parts[-1])
    return feat


def enrich(mbid):
    """Compatibility projection; probe() additionally preserves source observations."""
    return _features(*_read(mbid))


