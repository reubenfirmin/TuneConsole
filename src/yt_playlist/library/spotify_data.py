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


class SpotifyDataFormatError(ValueError):
    """The upload is not a recognizable Spotify streaming-history export."""


_ZIP_MAGIC = b"PK\x03\x04"
_MEMBER_CAP = 256 * 1024 * 1024
_TOTAL_CAP = 512 * 1024 * 1024
SOURCE = 'spotify_export'
MIN_LISTEN_MS = 30_000


def _parse_time(value):
    try:
        stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(datetime.timezone.utc).timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def _text(value):
    return value.strip() if isinstance(value, str) else ''


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
        title = _text(item.get("master_metadata_track_name"))
        artist = _text(item.get("master_metadata_album_artist_name"))
        uri = _text(item.get("spotify_track_uri"))
        ts = _parse_time(item.get("ts"))
        if not title or not artist or not uri.startswith("spotify:track:") or not uri[14:] or ts is None:
            skipped += 1
            continue
        duration = item.get('ms_played')
        duration = duration if type(duration) is int and duration >= 0 else None
        if any(item.get(field) is not None and type(item[field]) is not bool
               for field in ('skipped', 'incognito_mode')):
            skipped += 1
            continue
        # This is the entire in-memory whitelist. Device, location, IP, offline
        # identifiers and unrelated media metadata never reach persistence or logs.
        rows.append({"title": title, "artist": artist, "ts": ts, "spotify_uri": uri,
                     'ms_played': duration, 'skipped': item.get('skipped'),
                     'private': item.get('incognito_mode') is True,
                     'reason_start': _text(item.get('reason_start')),
                     'reason_end': _text(item.get('reason_end'))})
    return rows, skipped


def _load(raw):
    data = raw.encode() if isinstance(raw, str) else raw
    if not isinstance(data, (bytes, bytearray)):
        raise SpotifyDataFormatError("unsupported upload")
    if len(data) > _TOTAL_CAP:
        raise SpotifyDataFormatError("the streaming history is unreasonably large")
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
        try:
            with archive:
                for member in members:
                    with archive.open(member) as stream:
                        part, ignored = _parse_json(stream.read(_MEMBER_CAP + 1))
                    rows.extend(part)
                    skipped += ignored
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise SpotifyDataFormatError("the zip could not be read") from exc
    return rows, skipped


def prepare_streaming_history(raw):
    """Deduplicate before applying quality rules; return only eligible music listens.

    Spotify timestamps describe the end of a listen. Conflicting duplicate rows
    use the longest recorded duration, with deterministic metadata precedence.
    Any private copy in the upload excludes the whole occurrence. Skip flags never create a
    dislike: a skipped listen still counts when at least 30 seconds were heard.
    """
    rows, invalid = _load(raw)
    grouped, private_keys, variants = {}, set(), {}
    report = dict(invalid=invalid, duplicates=0, conflicting_duplicates=0,
                  private=0, short=0, unknown_duration=0)
    for row in rows:
        key = (row['spotify_uri'], row['ts'])
        if row['private']:
            private_keys.add(key)
        variants.setdefault(key, set()).add(json.dumps(row, sort_keys=True))
        previous = grouped.get(key)
        if previous is not None:
            report['duplicates'] += 1
            rank = lambda r: (r['ms_played'] if r['ms_played'] is not None else -1,
                              json.dumps(r, sort_keys=True))
            row = max((previous, row), key=rank)
        grouped[key] = row
    report['conflicting_duplicates'] = sum(len(copies) - 1 for copies in variants.values())
    eligible = []
    for key, row in grouped.items():
        if key in private_keys:
            report['private'] += 1
        elif row['ms_played'] is None:
            report['unknown_duration'] += 1
        elif row['ms_played'] < MIN_LISTEN_MS:
            report['short'] += 1
        else:
            eligible.append(row)
    eligible.sort(key=lambda row: (row['ts'], row['spotify_uri']))
    report['parsed'] = len(rows) + invalid
    report['eligible'] = len(eligible)
    report['skipped'] = invalid + report['private'] + report['short'] + report['unknown_duration']
    return eligible, report


def load_streaming_history(raw) -> tuple[list[dict], int]:
    """Return eligible, unique music listens and the count of excluded entries."""
    rows, report = prepare_streaming_history(raw)
    return rows, report['skipped']


def resolve_import_identity(store):
    """A local Spotify export belongs to the configured main identity, not a live YTM session."""
    identities = store.identities.get_identities()
    main = [identity for identity in identities if identity.is_master]
    candidates = main or identities
    return candidates[0].id if len(candidates) == 1 else None


def import_spotify_data(store, raw, *, on_progress=None) -> dict:
    """Parse, library-match, and idempotently backfill Spotify listening history."""
    ident = resolve_import_identity(store)
    if ident is None:
        return {"error": "no identity configured"}
    progress = on_progress or (lambda text: None)
    progress('Reading Spotify history…')
    parsed, quality = prepare_streaming_history(raw)
    progress(f'Matching {len(parsed):,} history entries to your library…')
    owned = set(store.rec_query.library_keys())
    matched, unmatched, listens, matches = [], Counter(), [], {}
    for row in parsed:
        credit = (row['title'], row['artist'])
        if credit not in matches:
            matches[credit] = store.tracks.match_recording(*credit)
        key = matches[credit]
        listens.append((row['spotify_uri'],row['ts'],row['artist'],key if key in owned else None))
        if key not in owned:
            unmatched[row["artist"]] += 1
            continue
        # Spotify ids are intentionally not put in the YouTube video_id column.
        matched.append((key, None, row["ts"]))
    progress(f'Saving {len(matched):,} matched plays…')
    plays_added,events_added,listens_added = store.history.import_spotify_listens(ident,listens)
    span_days = int((parsed[-1]["ts"] - parsed[0]["ts"]) // 86400) if parsed else 0
    return {**quality, "source": SOURCE, "identity_id": ident, "matched": len(matched), "unmatched": sum(unmatched.values()),
            "plays_added": plays_added, "events_added": events_added, "listens_added": listens_added,
            "span_days": span_days,
            "unmatched_artists": store.history.spotify_unmatched_artists(ident)}
