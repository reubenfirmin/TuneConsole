"""Local Spotify Extended Streaming History import.

Spotify's account-data export contains one or more ``Streaming_History_Audio_*.json`` files.
TuneConsole reads either the downloaded zip or one extracted JSON file locally.  Podcast and
audiobook rows are ignored; music rows are matched to the already-synced YouTube Music library by
normalized title and artist, just like Google Takeout's title/artist fallback.
"""
from __future__ import annotations

import datetime
import io
import json
import zipfile
from collections import Counter

from yt_playlist.library.live_plays import resolve_identity
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util.matching import identity_key


class SpotifyDataFormatError(ValueError):
    """The upload is not a recognizable Spotify streaming-history export."""


_ZIP_MAGIC = b"PK\x03\x04"
_MEMBER_CAP = 256 * 1024 * 1024
_TOTAL_CAP = 512 * 1024 * 1024


def _parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _parse_json(raw) -> tuple[list[dict], int]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise SpotifyDataFormatError("not valid JSON") from exc
    if not isinstance(data, list):
        raise SpotifyDataFormatError("expected a JSON list")
    rows, skipped = [], 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        # Extended history mixes tracks, podcast episodes, and audiobook chapters. A Spotify track
        # URI plus the track metadata fields is the stable discriminator documented in the export.
        title = (item.get("master_metadata_track_name") or "").strip()
        artist = (item.get("master_metadata_album_artist_name") or "").strip()
        uri = (item.get("spotify_track_uri") or "").strip()
        ts = _parse_time(item.get("ts"))
        if not title or not artist or not uri.startswith("spotify:track:") or ts is None:
            skipped += 1
            continue
        rows.append({"title": title, "artist": artist, "ts": ts, "spotify_uri": uri})
    return rows, skipped


def load_streaming_history(raw) -> tuple[list[dict], int]:
    """Return all music rows and the count of ignored/unreadable rows, oldest first."""
    data = raw.encode() if isinstance(raw, str) else raw
    if not isinstance(data, (bytes, bytearray)):
        raise SpotifyDataFormatError("unsupported upload")
    if not data.startswith(_ZIP_MAGIC):
        rows, skipped = _parse_json(data)
    else:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            members = [m for m in archive.infolist() if not m.is_dir()
                       and m.filename.rsplit("/", 1)[-1].lower().startswith("streaming_history_audio_")
                       and m.filename.lower().endswith(".json")]
        except zipfile.BadZipFile as exc:
            raise SpotifyDataFormatError("the zip could not be read") from exc
        if not members:
            raise SpotifyDataFormatError("no Spotify audio streaming history found in the zip")
        if any(m.file_size > _MEMBER_CAP for m in members) or sum(m.file_size for m in members) > _TOTAL_CAP:
            raise SpotifyDataFormatError("the streaming history is unreasonably large")
        rows, skipped = [], 0
        for member in members:
            with archive.open(member) as stream:
                part, ignored = _parse_json(stream.read())
            rows.extend(part)
            skipped += ignored
    rows.sort(key=lambda row: row["ts"])
    return rows, skipped


def import_spotify_data(store, raw) -> dict:
    """Parse, library-match, and idempotently backfill Spotify listening history."""
    ident = resolve_identity(store, "")
    if ident is None:
        return {"error": "no identity configured"}
    parsed, skipped = load_streaming_history(raw)
    owned = set(RecDao(store).library_keys())
    matched, unmatched = [], Counter()
    for row in parsed:
        key = identity_key(row["title"], row["artist"])
        if key not in owned:
            unmatched[row["artist"]] += 1
            continue
        # Spotify ids are intentionally not put in the YouTube video_id column.
        matched.append((key, None, row["ts"]))
    plays_added = store.import_plays(ident, [(key, ts) for key, _vid, ts in matched])
    events_added = store.import_play_events(ident, matched, source="spotify")
    span_days = int((parsed[-1]["ts"] - parsed[0]["ts"]) // 86400) if parsed else 0
    return {"matched": len(matched), "unmatched": sum(unmatched.values()),
            "plays_added": plays_added, "events_added": events_added, "span_days": span_days,
            "unmatched_artists": dict(unmatched), "skipped": skipped, "parsed": len(parsed)}
