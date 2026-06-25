"""AcousticBrainz enrichment: BPM, a derived energy, and danceability, keyed by MusicBrainz
recording MBID.

AcousticBrainz is a frozen (2022) but still-served CC0 dataset of Essentia acoustic descriptors. It
has no name-based lookup — every query is by MusicBrainz *recording* MBID — so for a track without a
stored MBID we resolve one live via the MusicBrainz provider, persist it, then query two endpoints:
low-level (rhythm.bpm) and high-level (mood/danceability classifiers). AcousticBrainz has no native
"energy" scalar, so we derive one from its mood models (see derive_energy). A 404 means "no data for
this MBID" (common, given frozen coverage) — a clean miss, not an outage. Failures degrade to Nones.
"""
import json
import logging
import threading
import time
import urllib.request

from yt_playlist.util import net
from yt_playlist.providers import musicbrainz
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.enrich_queue import PriorityGate

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
_pace_lock = threading.Lock()
_last_call = [0.0]
_gate = PriorityGate()
_breaker = net.CircuitBreaker()


def _get_json(url):
    with _pace_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:                  # report to the breaker (404 = reachable), then re-raise
        _breaker.record(e)
        raise
    _breaker.record()
    return data


def _prob(highlevel, model, label):
    try:
        return float(highlevel[model]["all"][label])
    except (KeyError, TypeError, ValueError):
        return None


def derive_energy(highlevel):
    """Heuristic energy in [0,1] from AcousticBrainz mood classifiers (no native energy field):
    0.5*party + 0.3*aggressive + 0.2*danceable. Returns None if the inputs are absent."""
    party = _prob(highlevel, "mood_party", "party")
    aggressive = _prob(highlevel, "mood_aggressive", "aggressive")
    danceable = _prob(highlevel, "danceability", "danceable")
    if party is None and aggressive is None and danceable is None:
        return None
    return round(0.5 * (party or 0.0) + 0.3 * (aggressive or 0.0) + 0.2 * (danceable or 0.0), 3)


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
    except Exception as e:  # noqa: BLE001
        logger.info("AcousticBrainz low-level miss for %s: %s", mbid, e)
    try:
        high = _get_json(f"{_API}/{mbid}/high-level").get("highlevel", {})
        feat["energy"] = derive_energy(high)
        feat["danceability"] = _prob(high, "danceability", "danceable")
        feat["mood_happy"] = _prob(high, "mood_happy", "happy")
        feat["mood_sad"] = _prob(high, "mood_sad", "sad")
        feat["mood_relaxed"] = _prob(high, "mood_relaxed", "relaxed")
        feat["mood_acoustic"] = _prob(high, "mood_acoustic", "acoustic")
        feat["instrumental"] = _prob(high, "voice_instrumental", "instrumental")
    except Exception as e:  # noqa: BLE001
        logger.info("AcousticBrainz high-level miss for %s: %s", mbid, e)
    return feat


def enrich_playlist(store, playlist_id, on_progress, enrich_fn=None, mbid_fn=None,
                    should_stop=None, pending=None):
    """Fill missing audio features for a track set from AcousticBrainz (fill-only). For tracks
    without a stored MusicBrainz MBID, resolve one via mbid_fn (default: musicbrainz.recording_mbid)
    and persist it. Scope is a playlist or an explicit `pending` list."""
    enrich_fn = enrich_fn or enrich
    mbid_fn = mbid_fn or musicbrainz.recording_mbid
    pending = store.tracks_missing_audio(playlist_id) if pending is None else pending
    total = len(pending)
    if not total:
        on_progress({"type": "done", "text": "Every track already has audio features.", "total": 0})
        return
    on_progress({"type": "info", "text": f"Fetching audio features for {total} track(s) "
                 "from AcousticBrainz…", "total": total})
    _breaker.reset()
    seq = _gate.enter()
    try:
        for i, t in enumerate(pending, 1):
            if should_stop and should_stop():
                on_progress({"type": "info", "text": "Stopped."})
                return
            _gate.wait_turn(seq, on_wait=lambda: on_progress(
                {"type": "info", "text": "Waiting — a newer playlist is looking up first…"}))
            mbid = t.get("mb_recording_id")
            if not mbid:
                mbid = mbid_fn(t["title"], t["artist"])
                if mbid:
                    store.set_track_mbid(t["id"], mbid)
            feat = enrich_fn(mbid) if mbid else _empty()
            if _breaker.tripped():
                on_progress({"type": "err", "text": "AcousticBrainz looks unreachable — stopped. "
                             "The remaining tracks will retry next time."})
                return
            store.set_track_audio(t["id"], **feat)
            bits = []
            if feat["bpm"]:
                bits.append(f"{feat['bpm']:.0f} BPM")
            if feat["energy"] is not None:
                bits.append(f"energy {feat['energy']:.2f}")
            if feat["music_key"]:
                bits.append(f"{feat['music_key']} {feat['music_scale'] or ''}".strip())
            shown = " · ".join(bits) or "no data"
            on_progress({"type": "track", "i": i, "n": total, "video_id": t["video_id"],
                         "text": f"{i}/{total} {t['title']} — {shown}"})
        on_progress({"type": "done", "text": f"Fetched {total} track(s).", "total": total})
    finally:
        _gate.leave(seq)
