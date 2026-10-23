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
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers import musicbrainz, transport
from yt_playlist.providers.base import EnrichmentResult, RateLimiter

logger = logging.getLogger(__name__)

name = "acousticbrainz"


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None, enrich_fn=None, mbid_fn=None) -> EnrichmentResult:
    """Read-only lookup: bpm, energy, danceability, keyed by MusicBrainz recording MBID. Reads a
    stored/earlier-resolved mb_recording_id off the track, else resolves one live via MusicBrainz."""
    enrich_fn = enrich_fn or enrich
    mbid_fn = mbid_fn or musicbrainz.recording_mbid
    mbid = track.get("mb_recording_id") or mbid_fn(track["title"], track["artist"])
    if not mbid:
        return EnrichmentResult("acousticbrainz", {})
    feat = enrich_fn(mbid)                       # dict of audio features, each value or None
    fields = {"mb_recording_id": mbid}
    fields.update({k: v for k, v in feat.items() if v is not None})
    return EnrichmentResult("acousticbrainz", fields)

_API = "https://acousticbrainz.org/api/v1"
_USER_AGENT = "yt-playlist/0.1 ( https://github.com/yt-playlist ; rf@4rc.io )"
_MIN_INTERVAL = 0.5                         # be a good citizen of a free community dataset
_HTTP_TIMEOUT_S = 20                        # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(_MIN_INTERVAL)
_breaker = net.CircuitBreaker()


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    return transport.request_json(req, timeout=_HTTP_TIMEOUT_S, pace=_pacer.wait, breaker=_breaker)


def _prob(highlevel, model, label):
    try:
        return float(highlevel[model]["all"][label])
    except (KeyError, TypeError, ValueError):
        return None


# Empirical blend (no external source) mapping AcousticBrainz mood models to a perceived-energy
# scalar: party drives the bulk of it, aggression adds intensity, danceability a smaller lift.
_ENERGY_W_PARTY = 0.5
_ENERGY_W_AGGRESSIVE = 0.3
_ENERGY_W_DANCEABLE = 0.2


def derive_energy(highlevel):
    """Heuristic energy in [0,1] from AcousticBrainz mood classifiers (no native energy field):
    a weighted blend of party/aggressive/danceable (see _ENERGY_W_*). Returns None if absent."""
    party = _prob(highlevel, "mood_party", "party")
    aggressive = _prob(highlevel, "mood_aggressive", "aggressive")
    danceable = _prob(highlevel, "danceability", "danceable")
    if party is None and aggressive is None and danceable is None:
        return None
    return round(_ENERGY_W_PARTY * (party or 0.0) + _ENERGY_W_AGGRESSIVE * (aggressive or 0.0)
                 + _ENERGY_W_DANCEABLE * (danceable or 0.0), 3)


_FIELDS = ("bpm", "energy", "danceability", "music_key", "music_scale", "mood_happy",
           "mood_sad", "mood_relaxed", "mood_acoustic", "instrumental", "loudness",
           "dynamic_complexity")


def _empty():
    return {k: None for k in _FIELDS}


def _num(d, key):
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def enrich(mbid):
    """Return a dict of audio features for a recording MBID (each value float/str or None).
    All-None when AcousticBrainz has no data for this MBID."""
    feat = _empty()
    if not mbid:
        return feat
    try:
        low = _get_json(f"{_API}/{mbid}/low-level")
        feat["bpm"] = _num(low.get("rhythm", {}), "bpm")
        tonal = low.get("tonal", {})
        feat["music_key"] = tonal.get("key_key") or None
        feat["music_scale"] = tonal.get("key_scale") or None
        ll = low.get("lowlevel", {})
        feat["loudness"] = _num(ll, "average_loudness")
        feat["dynamic_complexity"] = _num(ll, "dynamic_complexity")
    except Exception as e:  # noqa: BLE001 - sparse frozen dataset: misses are routine
        logger.debug("AcousticBrainz low-level miss for %s: %s", mbid, e)
    try:
        high = _get_json(f"{_API}/{mbid}/high-level").get("highlevel", {})
        feat["energy"] = derive_energy(high)
        feat["danceability"] = _prob(high, "danceability", "danceable")
        feat["mood_happy"] = _prob(high, "mood_happy", "happy")
        feat["mood_sad"] = _prob(high, "mood_sad", "sad")
        feat["mood_relaxed"] = _prob(high, "mood_relaxed", "relaxed")
        feat["mood_acoustic"] = _prob(high, "mood_acoustic", "acoustic")
        feat["instrumental"] = _prob(high, "voice_instrumental", "instrumental")
    except Exception as e:  # noqa: BLE001 - sparse frozen dataset: misses are routine
        logger.debug("AcousticBrainz high-level miss for %s: %s", mbid, e)
    return feat


