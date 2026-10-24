"""Import playlists and saved albums from Spotify's Account Data export."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from copy import deepcopy

from yt_playlist.library.album_fetch import shareable_album_tracks
from yt_playlist.library import album_writes
from yt_playlist.library.playlist_mutation import _read as _read_playlist
from yt_playlist.library.playlist_mutation import apply_playlist_permutation
from yt_playlist.util.matching import fuzzy_ratio, identity_key, normalize, track_artist
from yt_playlist.util.retry import with_retry
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.repos.base import GENERATED_GROUP

logger = logging.getLogger(__name__)


class SpotifyLibraryFormatError(ValueError):
    """The upload is not a Spotify Account Data library export."""


_ZIP_MAGIC = b"PK\x03\x04"
_MEMBER_CAP = 64 * 1024 * 1024
_TOTAL_CAP = 128 * 1024 * 1024
_PLAYLIST_STATE = "spotify_library_imported_playlists"
_ALBUM_STATE = "spotify_library_imported_albums"
SPOTIFY_REPORT_SCHEMA = 6
_RECORDING_RE = re.compile(r"\b(?:live|remix|remaster(?:ed)?|acoustic|instrumental|karaoke|demo|"
                           r"radio edit|clean|explicit|mono|stereo)\b", re.I)

_ALBUM_EDITION_RE = re.compile(
    r"\s+(?:(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+edition)?|"
    r"expanded(?:\s+edition)?|deluxe(?:\s+edition)?|special(?:\s+edition)?|"
    r"complete(?:\s+edition)?|remaster(?:ed)?(?:\s+\d{4})?|bonus(?:\s+edition)?)$",
    re.I,
)


def _json(raw, member):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SpotifyLibraryFormatError(f"{member} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SpotifyLibraryFormatError(f"{member} has an unexpected shape")
    return value


def _track(value):
    value = value or {}
    # Playlist files wrap the song in {"track": {...}}; YourLibrary uses flatter names.
    value = value.get("track") if isinstance(value.get("track"), dict) else value
    title = (value.get("trackName") or value.get("track") or value.get("name") or "").strip()
    artist = (value.get("artistName") or value.get("artist") or "").strip()
    album = (value.get("albumName") or value.get("album") or "").strip()
    uri = (value.get("trackUri") or value.get("uri") or "").strip()
    if not title or not artist:
        return None
    return {"title": title, "artist": artist, "album": album, "uri": uri}


def load_spotify_library(raw) -> dict:
    """Parse Account Data into normalized playlists and saved albums without exposing profile data."""
    data = raw.encode() if isinstance(raw, str) else raw
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(_ZIP_MAGIC):
        raise SpotifyLibraryFormatError("upload the Spotify Account Data zip")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SpotifyLibraryFormatError("the zip could not be read") from exc
    members = [m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith(".json")
               and (m.filename.rsplit("/", 1)[-1].lower().startswith("playlist")
                    or m.filename.rsplit("/", 1)[-1].lower().startswith("yourlibrary"))]
    if not members:
        raise SpotifyLibraryFormatError("no Spotify playlists or saved albums found")
    if any(m.file_size > _MEMBER_CAP for m in members) or sum(m.file_size for m in members) > _TOTAL_CAP:
        raise SpotifyLibraryFormatError("the Spotify library export is unreasonably large")

    playlists, albums = [], []
    for member in members:
        payload = _json(archive.read(member), member.filename)
        for p in payload.get("playlists") or []:
            if not isinstance(p, dict) or not (p.get("name") or "").strip():
                continue
            tracks = []
            for item in p.get("items") or []:
                parsed = _track(item) if isinstance(item, dict) else None
                if parsed:
                    tracks.append(parsed)
            playlists.append({"name": p["name"].strip(), "description": p.get("description") or "",
                              "tracks": tracks})
        for a in payload.get("albums") or []:
            if not isinstance(a, dict):
                continue
            title = (a.get("album") or a.get("albumName") or a.get("name") or "").strip()
            artist = (a.get("artist") or a.get("artistName") or "").strip()
            uri = (a.get("uri") or a.get("albumUri") or "").strip()
            if title and artist:
                album = {"title": title, "artist": artist, "uri": uri}
                year = str(a.get("releaseDate") or a.get("year") or "")[:4]
                if len(year) == 4 and year.isdecimal():
                    album["year"] = year
                albums.append(album)
    return {"playlists": playlists, "albums": albums}


def _fingerprint(prefix, *parts):
    body = "\0".join(str(p) for p in parts)
    return prefix + ":" + hashlib.sha256(body.encode()).hexdigest()


def _spotify_url(uri, kind):
    """Convert a canonical export URI into an open.spotify.com URL; reject other URI shapes."""
    prefix = f"spotify:{kind}:"
    item_id = uri[len(prefix):] if isinstance(uri, str) and uri.startswith(prefix) else ""
    return f"https://open.spotify.com/{kind}/{item_id}" if item_id.isalnum() else None


def _load_state(store, key):
    try:
        value = json.loads(store.settings.get_setting(key) or "[]")
        return set(value) if isinstance(value, list) else set()
    except (TypeError, ValueError):
        return set()


def _save_state(store, key, values):
    store.settings.set_setting(key, json.dumps(sorted(values)))


def _song_key(title, artist):
    return (normalize(title), normalize(artist),
            tuple(sorted({version.lower() for version in _RECORDING_RE.findall(title)})))


def _resolve_song(client, row):
    results = with_retry(lambda: client.search(f"{row['title']} {row['artist']}", filter="songs")) or []
    wanted = normalize(f"{row['title']} {row['artist']}")
    ranked = {}
    for result in results:
        video = result.get("videoId")
        if not video:
            continue
        score = fuzzy_ratio(wanted, normalize(f"{result.get('title', '')} {track_artist(result)}"))
        title_score = fuzzy_ratio(normalize(row["title"]), normalize(result.get("title", "")))
        artist_score = fuzzy_ratio(normalize(row["artist"]), normalize(track_artist(result)))
        # normalize() deliberately folds recording qualifiers for library identity. Importing a
        # recording needs a stricter check so a live/remixed version cannot become a studio track.
        source_versions = _song_key(row["title"], row["artist"])[2]
        result_versions = _song_key(result.get("title") or "", track_artist(result))[2]
        if score >= .95 and title_score >= .95 and artist_score >= .9 and source_versions == result_versions:
            ranked[video] = (score, result)
    candidates = sorted(ranked.values(), key=lambda item: -item[0])
    if not candidates:
        return {"status": "unmatched", "match": None}
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .02:
        return {"status": "ambiguous", "match": None}
    match = candidates[0][1]
    if match.get("isAvailable") is False or match.get("available") is False:
        return {"status": "unavailable", "match": None}
    return {"status": "matched", "match": match}


def prepare_spotify_library(parsed, client, *, store=None, identity_id=None):
    """Resolve selected recordings and releases before confirmation, without writes."""
    cache = {}
    playlists = []
    for playlist in parsed["playlists"]:
        rows = []
        for source in playlist["tracks"]:
            key = _song_key(source["title"], source["artist"])
            if key not in cache:
                try:
                    cache[key] = _resolve_song(client, source)
                except Exception:  # one unavailable lookup must not hide the other preview rows
                    logger.exception("Spotify playlist track lookup failed")
                    cache[key] = {"status": "lookup_failed", "match": None}
            rows.append({"source": source, **cache[key]})
        playlists.append({**playlist, "resolution": rows})
    state = _album_state(store, identity_id) if store is not None else (set(), set(), set())
    albums = []
    for source in parsed["albums"]:
        reason = _album_skip_reason(source, *state)
        resolution = {"status": "skipped", "reason": reason, "match": None, "candidates": []}
        if reason is None:
            try:
                match, candidates = _resolve_album(client, source)
                ambiguous = sum(candidate["confident"] for candidate in candidates) > 1
                resolution = {"status": "matched" if match else "ambiguous" if ambiguous else "unmatched",
                              "match": match, "candidates": candidates}
            except Exception:
                logger.exception("Spotify saved-album preview lookup failed")
                resolution = {"status": "lookup_failed", "match": None, "candidates": []}
        albums.append({**source, "resolution": resolution})
    return {"playlists": playlists, "albums": albums}


def _resolve_album(client, row):
    # Count all qualifying releases before limiting the displayed near misses.
    candidates = find_album_candidates(client, row, limit=None)
    confident = [candidate for candidate in candidates if candidate["confident"]]
    shown = [candidate for index, candidate in enumerate(candidates) if index < 5 or candidate["confident"]]
    return confident[0] if len(confident) == 1 else None, shown


def _album_title(title):
    """Edition-insensitive title for catalog matching; the displayed title remains untouched."""
    value = normalize(title)
    while True:
        shorter = _ALBUM_EDITION_RE.sub("", value).strip()
        if shorter == value:
            return value
        value = shorter


def _album_artist(artist):
    value = normalize(artist)
    return value[4:] if value.startswith("the ") else value


def find_album_candidates(client, row, limit=5):
    """Rank YTM album search results and retain the near misses for user resolution."""
    results = with_retry(lambda: client.search(f"{row['title']} {row['artist']}", filter="albums")) or []
    wanted_title = normalize(row["title"])
    wanted_base = _album_title(row["title"])
    wanted_artist = _album_artist(row["artist"])
    ranked = {}
    for result in results:
        browse = result.get("browseId")
        if not browse:
            continue
        artist = track_artist(result)
        result_title = normalize(result.get("title", ""))
        title_score = max(fuzzy_ratio(wanted_title, result_title),
                          fuzzy_ratio(wanted_base, _album_title(result.get("title", ""))))
        artist_score = fuzzy_ratio(wanted_artist, _album_artist(artist))
        score = .8 * title_score + .2 * artist_score
        confident = (artist_score >= .85
                     and (wanted_title == result_title or wanted_base == _album_title(result.get("title", ""))
                          or (title_score >= .92 and score >= .91)))
        year = str(result.get("year") or "")
        source_year = str(row.get("year") or "")
        if source_year and year and source_year != year:
            confident = False
        candidate = {
            "browse_id": browse, "title": result.get("title") or "Untitled album",
            "artist": artist, "thumbnail": best_thumb(result.get("thumbnails")),
            "year": year,
            "score": round(score, 3), "title_score": round(title_score, 3),
            "artist_score": round(artist_score, 3), "confident": confident,
        }
        if browse not in ranked or candidate["score"] > ranked[browse]["score"]:
            ranked[browse] = candidate
    return sorted(ranked.values(), key=lambda candidate: -candidate["score"])[:limit]


def _album_fingerprint(source):
    return _fingerprint("album", source.get("uri") or source.get("source_uri") or "|".join((
        normalize(source["title"]), normalize(source["artist"]))))


def _album_state(store, identity_id):
    saved = {(normalize(a.get("title")), normalize(a.get("artist")))
             for a in store.collection.get_saved_albums(identity_id=identity_id)}
    editions = {(_album_title(title), _album_artist(artist)) for title, artist in saved}
    return _load_state(store, f"{_ALBUM_STATE}:{identity_id}"), saved, editions


def _album_skip_reason(source, markers, saved, editions):
    if _album_fingerprint(source) in markers:
        return "previously_imported"
    if ((normalize(source["title"]), normalize(source["artist"])) in saved
            or (_album_title(source["title"]), _album_artist(source["artist"])) in editions):
        return "already_in_library"
    return None


def _save_album(store, client, candidate, identity_id):
    """Use the existing YouTube album-like operation and retain YouTube catalog metadata."""
    album = with_retry(lambda: client.get_album(candidate["browse_id"])) or {}
    album_writes.set_saved(store, client, identity_id, candidate['browse_id'], album,
                          saved=True, fallback=candidate)


def _album_as_playlist(store, client, source, candidate, identity_id, now):
    """Resume an explicit album-to-playlist transfer using the existing playlist journal."""
    album = with_retry(lambda: client.get_album(candidate["browse_id"])) or {}
    tracks = shareable_album_tracks(album)
    playlist = {"name": album.get("title") or candidate["title"], "tracks": [], "resolution": []}
    for track in tracks:
        row = {"title": track.get("title") or "", "artist": track_artist(track), "uri": ""}
        playlist["tracks"].append(row)
        playlist["resolution"].append({"source": row, "status": "matched", "match": track})
    fingerprint = _fingerprint("album_playlist", _album_fingerprint(source), candidate["browse_id"])
    return _transfer_playlist(store, client, identity_id, playlist, fingerprint, now)


def _playlist_transfers(store, identity_id):
    key = f"{_PLAYLIST_STATE}:{identity_id}"
    value = json.loads(store.settings.get_setting(key) or "{}")
    return key, value


class _PlaylistPublication:
    """One import's confirmed prefix, normalized once and published atomically."""
    def __init__(self, store, identity_id, remote_id, title, now):
        self.store, self.destination = store, (identity_id, remote_id)
        self.title, self.now = title, now
        self.playlist_id = None
        self.previous = []
        self.cache = {}

    def publish(self, tracks):
        normalized, seen = [], set()
        for track in tracks:
            video = track.get('videoId')
            if not video or video in seen:
                continue
            seen.add(video)
            cached = self.cache.get(video)
            if cached is None or cached[0] != track:
                album = track.get('album') or {}
                row = {'video_id': video, 'title': track.get('title') or '', 'artist': track_artist(track),
                       'album': album.get('name', '') if isinstance(album, dict) else album,
                       'duration': track.get('duration_seconds'), 'thumbnail': best_thumb(track.get('thumbnails'))}
                cached = self.cache[video] = (deepcopy(track), row)
            normalized.append(cached[1])
        # Metadata changes and reordering require a full reconciliation. The
        # repository also verifies that no independent local edit changed order.
        prefix = ([row['video_id'] for row in self.previous]
                  if normalized[:len(self.previous)] == self.previous else None)
        self.playlist_id = self.store.playlists.publish_confirmed_tracks(
            self.playlist_id, self.title, normalized, self.now,
            expected_prefix=prefix, destination=self.destination)
        self.previous = normalized


def _order_imported_tracks(client, remote_id, source_videos):
    """Fill the imported songs' existing slots in source order, retaining all other occurrences.

    Destination additions keep their slots and relative order. Only imported occurrences move;
    no removals or playlist replacement are used. A concurrent edit defers completion to a retry.
    """
    tracks = _read_playlist(client, remote_id)
    occurrences = {}
    for index, track in enumerate(tracks):
        occurrences.setdefault(track["videoId"], index)
    if any(video not in occurrences for video in source_videos):
        raise ValueError("Imported songs are not all visible on YouTube yet")
    managed = {occurrences[video] for video in source_videos}
    ordered = iter(occurrences[video] for video in source_videos)
    target = [next(ordered) if index in managed else index for index in range(len(tracks))]
    apply_playlist_permutation(client, remote_id, tracks, target, movable=managed)
    confirmed = _read_playlist(client, remote_id)
    if [t["videoId"] for t in confirmed] != [tracks[index]["videoId"] for index in target]:
        raise ValueError("Imported playlist order is still unresolved; retry the import")
    return confirmed


def _transfer_playlist(store, client, identity_id, playlist, fingerprint, now):
    state_key, transfers = _playlist_transfers(store, identity_id)
    transfer = transfers.get(fingerprint)
    resumed = transfer is not None
    if transfer is None:
        remote_id = client.create_playlist(playlist["name"], "Imported from Spotify by TuneConsole")
        # Save the remote identity before the first add. A failed or lost batch response can then
        # be reconciled against that same remote playlist, even after restarting the application.
        transfer = {"ytm_playlist_id": remote_id, "complete": False, "updated_at": now, "last_run_added": 0}
        transfers[fingerprint] = transfer
        store.settings.set_setting(state_key, json.dumps(transfers))
        tracks = []
    else:
        remote_id = transfer["ytm_playlist_id"]
        transfer["last_run_added"] = 0
        store.settings.set_setting(state_key, json.dumps(transfers))
        tracks = _read_playlist(client, remote_id)
    # Retain the source slot for every accepted match, including across failed lookups on a retry.
    positions = transfer.setdefault("source_video_ids", [None] * len(playlist["resolution"]))
    for index, row in enumerate(playlist["resolution"]):
        if row["match"] and positions[index] is None:
            positions[index] = row["match"]["videoId"]
    intended = list(dict.fromkeys(video for video in positions if video))
    transfer.update(complete=False, order_verified=False)
    store.settings.set_setting(state_key, json.dumps(transfers))
    publication = _PlaylistPublication(store, identity_id, remote_id, playlist['name'], now)
    publication.publish(tracks)
    present = {t.get("videoId") for t in tracks}
    pending = [video for video in intended if video not in present]
    added = 0
    for start in range(0, len(pending), 100):
        batch = pending[start:start + 100]
        result = client.add_playlist_items(remote_id, batch)
        if not isinstance(result, dict) or result.get("status") != "STATUS_SUCCEEDED":
            raise RuntimeError("YouTube Music rejected a playlist batch")
        tracks = _read_playlist(client, remote_id)
        if not set(batch) <= {t["videoId"] for t in tracks}:
            raise ValueError("YouTube has not confirmed the imported playlist batch")
        publication.publish(tracks)
        added += len(batch)
        transfer["last_run_added"] = added
        store.settings.set_setting(state_key, json.dumps(transfers))
    tracks = _order_imported_tracks(client, remote_id, intended)
    publication.publish(tracks)
    transfer.update(complete=not any(row["status"] == "lookup_failed"
                                     for row in playlist["resolution"]), order_verified=True, updated_at=now)
    store.settings.set_setting(state_key, json.dumps(transfers))
    return remote_id, added, resumed


def import_spotify_library(store, raw, client, identity_id, now, *, prepared=None) -> dict:
    """Create missing YTM playlists/save albums. Re-importing the same source is idempotent."""
    parsed = prepared if prepared is not None else prepare_spotify_library(
        load_spotify_library(raw), client, store=store, identity_id=identity_id)
    _, playlist_state = _playlist_transfers(store, identity_id)
    legacy_state = _load_state(store, _PLAYLIST_STATE)
    album_state_key = f'{_ALBUM_STATE}:{identity_id}'
    report = {"playlists_found": len(parsed["playlists"]), "playlists_imported": 0,
              "playlists_skipped": 0, "playlists_failed": 0, "tracks_imported": 0, "tracks_unmatched": 0,
              "albums_found": len(parsed["albums"]), "albums_imported": 0,
              "albums_skipped": 0, "albums_unmatched": 0, "albums_as_playlists": 0,
              "details": {"playlists": [], "unmatched_tracks": [], "albums": []}}

    # Source fingerprints make retries safe even when some tracks could not be resolved. Also
    # compare exact title+contents against playlists that predate this importer, so an equivalent
    # YouTube Music playlist is not recreated merely because it has no Spotify import marker.
    existing_playlists = set()
    for existing in store.playlists.get_playlists():
        if (existing.identity_id != identity_id or existing.ytm_playlist_id == "LM"
                or existing.ytm_playlist_id.startswith("RD")
                or store.playlists.is_quarantined_playlist(existing.scope_ref)):
            continue
        rows = store.playlists.get_playlist_tracks_with_meta(existing.id)
        keys = frozenset(_song_key(t[2], t[3]) for t in rows)
        title_key = normalize(existing.title)
        existing_playlists.add((title_key, keys))

    for playlist in parsed["playlists"]:
        track_keys = [_song_key(t["title"], t["artist"]) for t in playlist["tracks"]]
        source_ids = [t["uri"] or str(key) for t, key in zip(playlist["tracks"], track_keys)]
        fingerprint = _fingerprint("playlist", normalize(playlist["name"]), *source_ids)
        legacy_fingerprint = _fingerprint("playlist", normalize(playlist["name"]), *(
            identity_key(t["title"], t["artist"]) for t in playlist["tracks"]))
        source_signature = (normalize(playlist["name"]), frozenset(track_keys))
        transfer = playlist_state.get(fingerprint)
        if transfer and transfer["complete"] and transfer.get("order_verified"):
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "previously_imported",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": 0})
            continue
        if transfer is None and source_signature in existing_playlists:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "already_exists",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": 0})
            continue
        if not playlist["tracks"]:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "empty",
                "tracks_found": 0, "tracks_added": 0, "tracks_unmatched": 0})
            continue
        resolved = []
        unmatched = []
        for row in playlist["resolution"]:
            source, match = row["source"], row["match"]
            if match:
                resolved.append((source, match))
            else:
                report["tracks_unmatched"] += 1
                detail = {"title": source["title"], "artist": source["artist"],
                          "album": source["album"], "playlist": playlist["name"],
                          "spotify_url": _spotify_url(source["uri"], "track"),
                          "reason": row["status"]}
                unmatched.append(detail)
                report["details"]["unmatched_tracks"].append(detail)
        lookup_failed = any(row["status"] == "lookup_failed" for row in playlist["resolution"])
        if not resolved:
            if lookup_failed:
                report["playlists_failed"] += 1
                report["details"]["playlists"].append({
                    "name": playlist["name"], "status": "failed", "reason": "lookup_failed",
                    "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                    "tracks_unmatched": len(unmatched)})
                continue
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "no_tracks_matched",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": len(unmatched)})
            continue
        resolved_signature = (source_signature[0], frozenset(
            _song_key(match.get("title", ""), track_artist(match)) for _, match in resolved))
        # Older versions stored a global fingerprint list. Trust it only when the destination
        # actually contains the corresponding playlist; another account's marker cannot suppress it.
        if (transfer is None and legacy_fingerprint in legacy_state and not lookup_failed
                and resolved_signature in existing_playlists):
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "previously_imported",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": len(unmatched)})
            continue
        try:
            new_id, added, resumed = _transfer_playlist(
                store, client, identity_id, playlist, fingerprint, now)
        except Exception:
            logger.exception("Spotify playlist transfer failed")
            _, current = _playlist_transfers(store, identity_id)
            failed = {"name": playlist["name"], "status": "failed", "reason": "transfer_failed",
                      "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                      "tracks_unmatched": len(unmatched)}
            if fingerprint in current:
                failed["ytm_playlist_id"] = current[fingerprint]["ytm_playlist_id"]
                failed["tracks_added"] = current[fingerprint].get("last_run_added", 0)
                report["tracks_imported"] += failed["tracks_added"]
            report["details"]["playlists"].append(failed)
            report["playlists_failed"] += 1
            continue
        if lookup_failed:
            report["playlists_failed"] += 1
        else:
            report["playlists_imported"] += 1
        report["tracks_imported"] += added
        report["details"]["playlists"].append({
            "name": playlist["name"], "status": "partial" if lookup_failed else "imported",
            "reason": "lookup_failed" if lookup_failed else ("resumed" if resumed else "created"),
            "tracks_found": len(playlist["tracks"]), "tracks_added": added,
            "tracks_unmatched": len(unmatched), "ytm_playlist_id": new_id})
        _, playlist_state = _playlist_transfers(store, identity_id)
        if not lookup_failed:
            existing_playlists.add(source_signature)

    album_state, saved, saved_editions = _album_state(store, identity_id)
    for source in parsed["albums"]:
        key = (normalize(source["title"]), normalize(source["artist"]))
        edition_key = (_album_title(source["title"]), _album_artist(source["artist"]))
        fingerprint = _album_fingerprint(source)
        source_detail = {"title": source["title"], "artist": source["artist"],
                         "source_uri": source["uri"],
                         "spotify_url": _spotify_url(source["uri"], "album")}
        resolution = source["resolution"]
        action = source.get("action", "save")
        reason = _album_skip_reason(source, album_state, saved, saved_editions)
        if action == "skip" or (reason and action != "playlist"):
            report["albums_skipped"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "skipped",
                "reason": "not_selected" if action == "skip" else reason})
            continue
        match, candidates = resolution["match"], resolution["candidates"]
        if not match:
            report["albums_unmatched"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "unmatched", "candidates": candidates,
                "reason": {"ambiguous": "ambiguous", "lookup_failed": "lookup_or_save_failed"}.get(
                    resolution["status"], "no_confident_match")})
            continue
        try:
            if action == "playlist":
                remote_id, added, _ = _album_as_playlist(store, client, source, match, identity_id, now)
                report["albums_as_playlists"] += 1
                report["details"]["albums"].append({
                    **source_detail, "status": "imported", "reason": "created_as_playlist",
                    "browse_id": match["browse_id"], "ytm_playlist_id": remote_id})
                report["tracks_imported"] += added
                continue
            _save_album(store, client, match, identity_id)
        except Exception:  # an optional album must not hide completed playlist results
            logger.exception("Spotify saved-album transfer failed")
            report["albums_unmatched"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "unmatched", "reason": "lookup_or_save_failed",
                "candidates": candidates})
            continue
        saved.add(key)
        saved_editions.add(edition_key)
        album_state.add(fingerprint)
        _save_state(store, album_state_key, album_state)
        report["albums_imported"] += 1
        report["details"]["albums"].append({
            **source_detail, "status": "imported",
            "reason": "saved", "browse_id": match["browse_id"]})
    return report


def save_selected_album(store, client, item, browse_id, *, identity_id):
    """Save one user-selected candidate and record its Spotify source for future idempotence."""
    candidate = next((c for c in item.get("candidates") or [] if c.get("browse_id") == browse_id), None)
    if candidate is None:
        raise ValueError("candidate is not part of this report")
    if identity_id not in {i.id for i in store.identities.get_identities()}:
        raise ValueError('Unknown or inactive destination account')
    already_saved = browse_id in {a.get("browse") for a in store.collection.get_saved_albums(identity_id=identity_id)}
    if not already_saved:
        _save_album(store, client, candidate, identity_id)
    state_key = f'{_ALBUM_STATE}:{identity_id}'
    state = _load_state(store, state_key)
    state.add(_album_fingerprint(item))
    _save_state(store, state_key, state)
    return {"status": "skipped" if already_saved else "imported",
            "reason": "already_in_library" if already_saved else "saved_by_user",
            "browse_id": browse_id}
