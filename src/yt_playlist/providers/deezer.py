"""Deezer enrichment: tempo (BPM) for a track, free and without authentication.

Deezer's public API needs no API key for catalog reads. We search several candidates, verify their
recording identity, then use a track lookup whose object carries the
`bpm` field (search/listing responses omit it). Deezer reports bpm=0 for tracks it hasn't analysed,
which we treat as unknown. Deezer also signals quota/errors via an {"error": ...} body on an HTTP
200, so we check for that explicitly. Failures degrade to None so one bad track never stops a run.
"""
import logging
import math
import re
import urllib.parse
import urllib.request

from yt_playlist.util import net
from yt_playlist.util.matching import normalize, recording_title
from yt_playlist.providers.base import EnrichmentResult, RateLimiter
from yt_playlist.providers import transport

logger = logging.getLogger(__name__)

name = "deezer"


def available(store=None) -> bool:
    return True


def tripped() -> bool:
    return _breaker.tripped()


def reset() -> None:
    _breaker.reset()


def probe(track, store=None) -> EnrichmentResult:
    """Expose supported catalogue facts from the shared lookup."""
    feat = lookup(track["title"], track["artist"], duration=track.get("duration_s"),
                  isrc=track.get("isrc"))
    fields = {k: feat[k] for k in (*_FIELDS, "genre", "year") if feat.get(k) is not None}
    if "year" in fields:
        fields["year"] = str(fields["year"])  # same discrete value type as the other providers
    return EnrichmentResult("deezer", fields)

_API = "https://api.deezer.com"
_USER_AGENT = "yt-playlist/0.1 +https://4rc.io"
_MIN_INTERVAL = 0.15                       # ~50 req / 5s soft limit -> stay well under
_HTTP_TIMEOUT_S = 20                        # cap each request so a stalled socket can't wedge a run
_pacer = RateLimiter(_MIN_INTERVAL)
_breaker = net.CircuitBreaker()


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    data = transport.request_json(req, timeout=_HTTP_TIMEOUT_S, pace=_pacer.wait, breaker=_breaker)
    if isinstance(data, dict) and data.get("error"):
        # Deezer reports quota/API errors as HTTP 200. Code 800 means no catalogue entry.
        if data["error"].get("code") != 800:
            net.record_probe_failure("Deezer API error")
    return data


_FIELDS = ("bpm", "popularity", "gain", "label", "art")
# Catalogue facts the same two lookups already pay for, alongside the audio fields: release year,
# genre and duration. Road Trip reads these off `lookup` to place a YouTube-sourced track on the era/genre
# sliders and to budget the mix by length without a get_song call per track.
_EXTRA_FIELDS = ("year", "genre", "duration")


def _empty(fields=_FIELDS):
    return {k: None for k in fields}


def _year(value):
    """Leading 4-digit year of a Deezer release_date ('1997-06-16'), or None."""
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def _title(track):
    title = recording_title(track.get("title"))
    version = recording_title(f"({track.get('title_version') or ''})")
    # Deezer can put the qualifier in a separate field instead of the displayed title. Unlike
    # search normalization, recording_title retains live/remix/edit qualifiers and folds remasters.
    if version and not title.endswith(version):
        title = f"{title} {version}"
    return title


def _matches(track, title, artist):
    wanted = normalize(artist)
    if not wanted or not recording_title(title) or _title(track) != recording_title(title):
        return False
    names = [(track.get("artist") or {}).get("name") or ""]
    names.extend(c.get("name") or "" for c in track.get("contributors") or [])
    # Deezer supplies individual contributor names, without MusicBrainz's aliases/join phrases.
    return wanted in {normalize(n) for n in (*names, " & ".join(names[1:]))}


def _seconds(value):
    try:
        number = float(value)
        return number if number > 0 and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _isrcs(value):
    values = [value] if isinstance(value, str) else (value or [])
    return {re.sub(r"[^A-Z0-9]", "", str(v).upper()) for v in values if v}


def _select_track(data, title, artist, duration, isrc):
    matches, seen = [], set()
    wanted_duration, wanted_isrc = _seconds(duration), _isrcs(isrc)
    for candidate in data[:10]:
        track_id = candidate.get("id")
        if track_id is None or track_id in seen or _title(candidate) != recording_title(title):
            continue
        seen.add(track_id)
        try:
            track = _get_json(_API + f"/track/{track_id}")
        except Exception as exc:
            logger.warning("Deezer track lookup failed for %s: %s", track_id, exc)
            return None  # an unread candidate cannot establish an unambiguous selection
        if not isinstance(track, dict) or track.get("error"):
            return None
        if track.get("id") != track_id or not _matches(track, title, artist):
            continue
        if wanted_isrc and not wanted_isrc.intersection(_isrcs(track.get("isrc"))):
            continue
        seconds = _seconds(track.get("duration"))
        if wanted_duration and seconds and abs(wanted_duration - seconds) > max(3, wanted_duration * .02):
            continue
        matches.append(track)
    if len(matches) == 1:
        return matches[0]
    # Different releases with the same explicit recording identifier are equivalent here.
    # Equal titles/durations alone cannot disambiguate distinct recordings or missing evidence.
    identifiers = [_isrcs(track.get("isrc")) for track in matches]
    if identifiers and all(identifiers) and set.intersection(*identifiers):
        return matches[0]
    return None


def lookup(title, artist, *, duration=None, isrc=None):
    """Verified catalogue facts, or unknown values if identity is missing or ambiguous.

    Duration (seconds) supports the title/artist check; it never overrides a version mismatch.
    A supplied ISRC must match. Legacy callers without either still require explicit artist/title.
    """
    feat = _empty(_FIELDS + _EXTRA_FIELDS)
    if not recording_title(title) or not normalize(artist):
        return feat
    q = " ".join(p for p in (f'artist:"{artist}"' if artist else "",
                             f'track:"{title}"' if title else "") if p)
    search_url = _API + "/search/track?" + urllib.parse.urlencode({"q": q, "limit": "10"})
    try:
        res = _get_json(search_url)
    except Exception as e:  # noqa: BLE001
        logger.warning("Deezer search failed for %r / %r: %s", title, artist, e)
        return feat
    if isinstance(res, dict) and res.get("error"):
        logger.warning("Deezer error for %r / %r: %s", title, artist, res["error"])
        return feat
    data = (res or {}).get("data") or []
    if not data:
        return feat
    track = _select_track(data, title, artist, duration, isrc)
    if track is None:
        return feat
    try:
        bpm = float(track.get("bpm"))
        feat["bpm"] = bpm if bpm > 0 else None     # Deezer reports 0 for un-analysed tracks
    except (TypeError, ValueError):
        feat["bpm"] = None
    rank = track.get("rank")
    feat["popularity"] = int(rank) if isinstance(rank, (int, float)) else None
    try:
        gain = track.get("gain")
        feat["gain"] = float(gain) if gain is not None else None
    except (TypeError, ValueError):
        feat["gain"] = None
    # The cover is embedded in the /track response, so it costs no extra request. YouTube has no art
    # at all for an upload (it serves a generic grey disc), and Deezer knows these tracks: they are
    # ordinary catalogue songs, whatever compilation you happened to rip them onto.
    try:
        secs = int(track.get("duration"))
        feat["duration"] = secs if secs > 0 else None
    except (TypeError, ValueError):
        feat["duration"] = None
    feat["year"] = _year(track.get("release_date"))
    track_album = track.get("album") or {}
    feat["art"] = track_album.get("cover_xl") or track_album.get("cover_big") or None
    album_id = track_album.get("id")
    if album_id is not None:
        try:
            album = _get_json(_API + f"/album/{album_id}")
            if isinstance(album, dict) and not album.get("error"):
                feat["label"] = album.get("label") or None
                # A track carries no genre of its own on Deezer; its album does.
                genres = ((album.get("genres") or {}).get("data")) or []
                feat["genre"] = (genres[0].get("name") or None) if genres else None
                feat["year"] = feat["year"] or _year(album.get("release_date"))
        except Exception as e:  # noqa: BLE001
            logger.info("Deezer album lookup failed for %s: %s", album_id, e)
    return feat


