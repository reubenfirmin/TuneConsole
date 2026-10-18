"""Import playlists and saved albums from Spotify's Account Data export."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from yt_playlist.library.sync import content_hash
from yt_playlist.util.matching import fuzzy_ratio, identity_key, normalize, track_artist
from yt_playlist.util.retry import with_retry
from yt_playlist.util.thumbnails import best_thumb


class SpotifyLibraryFormatError(ValueError):
    """The upload is not a Spotify Account Data library export."""


_ZIP_MAGIC = b"PK\x03\x04"
_MEMBER_CAP = 64 * 1024 * 1024
_TOTAL_CAP = 128 * 1024 * 1024
_PLAYLIST_STATE = "spotify_library_imported_playlists"
_ALBUM_STATE = "spotify_library_imported_albums"
SPOTIFY_REPORT_SCHEMA = 4

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
                albums.append({"title": title, "artist": artist, "uri": uri})
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
        value = json.loads(store.get_setting(key) or "[]")
        return set(value) if isinstance(value, list) else set()
    except (TypeError, ValueError):
        return set()


def _save_state(store, key, values):
    store.set_setting(key, json.dumps(sorted(values)))


def _resolve_song(client, row):
    results = with_retry(lambda: client.search(f"{row['title']} {row['artist']}", filter="songs")) or []
    wanted = normalize(f"{row['title']} {row['artist']}")
    best = None
    for result in results:
        video = result.get("videoId")
        if not video:
            continue
        score = fuzzy_ratio(wanted, normalize(f"{result.get('title', '')} {track_artist(result)}"))
        if score >= .95 and (best is None or score > best[0]):
            best = (score, result)
    return best[1] if best else None


def _resolve_album(client, row):
    candidates = find_album_candidates(client, row)
    return next((candidate for candidate in candidates if candidate["confident"]), None), candidates


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
    ranked = []
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
        ranked.append({
            "browse_id": browse, "title": result.get("title") or "Untitled album",
            "artist": artist, "thumbnail": best_thumb(result.get("thumbnails")),
            "score": round(score, 3), "title_score": round(title_score, 3),
            "artist_score": round(artist_score, 3), "confident": confident,
        })
    ranked.sort(key=lambda candidate: -candidate["score"])
    return ranked[:limit]


def _add_items(client, playlist_id, video_ids):
    for start in range(0, len(video_ids), 100):
        client.add_playlist_items(playlist_id, video_ids[start:start + 100])


def import_spotify_library(store, raw, client, identity_id, now) -> dict:
    """Create missing YTM playlists/save albums. Re-importing the same source is idempotent."""
    parsed = load_spotify_library(raw)
    playlist_state = _load_state(store, _PLAYLIST_STATE)
    album_state = _load_state(store, _ALBUM_STATE)
    report = {"playlists_found": len(parsed["playlists"]), "playlists_imported": 0,
              "playlists_skipped": 0, "tracks_imported": 0, "tracks_unmatched": 0,
              "albums_found": len(parsed["albums"]), "albums_imported": 0,
              "albums_skipped": 0, "albums_unmatched": 0,
              "details": {"playlists": [], "unmatched_tracks": [], "albums": []}}

    # Source fingerprints make retries safe even when some tracks could not be resolved. Also
    # compare exact title+contents against playlists that predate this importer, so an equivalent
    # YouTube Music playlist is not recreated merely because it has no Spotify import marker.
    existing_playlists = set()
    existing_playlist_titles = set()
    for existing in store.get_playlists():
        rows = store.get_playlist_tracks_with_meta(existing.id)
        keys = tuple(t[0] for t in rows)  # identity_key is the first field of the repository tuple
        title_key = normalize(existing.title)
        existing_playlist_titles.add(title_key)
        existing_playlists.add((title_key, keys))

    song_cache = {}
    for playlist in parsed["playlists"]:
        track_keys = [identity_key(t["title"], t["artist"]) for t in playlist["tracks"]]
        fingerprint = _fingerprint("playlist", normalize(playlist["name"]), *track_keys)
        source_signature = (normalize(playlist["name"]), tuple(track_keys))
        if fingerprint in playlist_state:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "previously_imported",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": 0})
            continue
        if source_signature in existing_playlists:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "already_exists",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": 0})
            continue
        if source_signature[0] in existing_playlist_titles:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "name_already_exists",
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
        for source in playlist["tracks"]:
            key = identity_key(source["title"], source["artist"])
            if key not in song_cache:
                song_cache[key] = _resolve_song(client, source)
            match = song_cache[key]
            if match:
                resolved.append((source, match))
            else:
                report["tracks_unmatched"] += 1
                detail = {"title": source["title"], "artist": source["artist"],
                          "album": source["album"], "playlist": playlist["name"],
                          "spotify_url": _spotify_url(source["uri"], "track")}
                unmatched.append(detail)
                report["details"]["unmatched_tracks"].append(detail)
        if not resolved:
            report["playlists_skipped"] += 1
            report["details"]["playlists"].append({
                "name": playlist["name"], "status": "skipped", "reason": "no_tracks_matched",
                "tracks_found": len(playlist["tracks"]), "tracks_added": 0,
                "tracks_unmatched": len(unmatched)})
            continue
        new_id = client.create_playlist(playlist["name"], "Imported from Spotify by TuneConsole")
        video_ids = list(dict.fromkeys(match["videoId"] for _source, match in resolved))
        _add_items(client, new_id, video_ids)
        track_ids, keys = [], []
        by_video = {match["videoId"]: (source, match) for source, match in resolved}
        for video in video_ids:
            source, match = by_video[video]
            artist = track_artist(match) or source["artist"]
            album = match.get("album")
            album = album.get("name") if isinstance(album, dict) else source["album"]
            track_ids.append(store.upsert_track(video, match.get("title") or source["title"], artist,
                                                album or "", match.get("duration_seconds"),
                                                thumbnail=best_thumb(match.get("thumbnails"))))
            keys.append(identity_key(match.get("title") or source["title"], artist))
        local_id = store.upsert_playlist(identity_id, new_id, playlist["name"], len(track_ids),
                                         content_hash(keys), now)
        store.set_playlist_tracks(local_id, track_ids)
        store.ensure_playlist_thumbnail(local_id)
        playlist_state.add(fingerprint)
        existing_playlists.add(source_signature)
        existing_playlist_titles.add(source_signature[0])
        _save_state(store, _PLAYLIST_STATE, playlist_state)
        report["playlists_imported"] += 1
        report["tracks_imported"] += len(track_ids)
        report["details"]["playlists"].append({
            "name": playlist["name"], "status": "imported", "reason": "created",
            "tracks_found": len(playlist["tracks"]), "tracks_added": len(track_ids),
            "tracks_unmatched": len(unmatched), "ytm_playlist_id": new_id})

    saved = {(normalize(a.get("title")), normalize(a.get("artist")))
             for a in store.get_saved_albums()}
    saved_editions = {(_album_title(title), _album_artist(artist)) for title, artist in saved}
    for source in parsed["albums"]:
        key = (normalize(source["title"]), normalize(source["artist"]))
        edition_key = (_album_title(source["title"]), _album_artist(source["artist"]))
        fingerprint = _fingerprint("album", source["uri"] or "|".join(key))
        source_detail = {"title": source["title"], "artist": source["artist"],
                         "source_uri": source["uri"],
                         "spotify_url": _spotify_url(source["uri"], "album")}
        if fingerprint in album_state:
            report["albums_skipped"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "skipped",
                "reason": "previously_imported"})
            continue
        if key in saved or edition_key in saved_editions:
            report["albums_skipped"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "skipped",
                "reason": "already_in_library"})
            continue
        match, candidates = _resolve_album(client, source)
        if not match:
            report["albums_unmatched"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "unmatched",
                "reason": "no_confident_match", "candidates": candidates})
            continue
        album = with_retry(lambda: client.get_album(match["browse_id"])) or {}
        audio_id = album.get("audioPlaylistId")
        if not audio_id:
            report["albums_unmatched"] += 1
            report["details"]["albums"].append({
                **source_detail, "status": "unmatched",
                "reason": "not_saveable", "browse_id": match["browse_id"]})
            continue
        client.rate_playlist(audio_id, "LIKE")
        artist = ", ".join(a.get("name", "") for a in (album.get("artists") or []) if a.get("name"))
        store.add_saved_album({"browse": match["browse_id"], "title": album.get("title") or source["title"],
                               "artist": artist or source["artist"], "year": album.get("year"),
                               "type": album.get("type"), "thumbnail": best_thumb(album.get("thumbnails"))})
        saved.add(key)
        saved_editions.add(edition_key)
        album_state.add(fingerprint)
        _save_state(store, _ALBUM_STATE, album_state)
        report["albums_imported"] += 1
        report["details"]["albums"].append({
            **source_detail, "status": "imported",
            "reason": "saved", "browse_id": match["browse_id"]})
    return report


def save_selected_album(store, client, item, browse_id):
    """Save one user-selected candidate and record its Spotify source for future idempotence."""
    candidate = next((c for c in item.get("candidates") or [] if c.get("browse_id") == browse_id), None)
    if candidate is None:
        raise ValueError("candidate is not part of this report")
    already_saved = browse_id in {a.get("browse") for a in store.get_saved_albums()}
    album = with_retry(lambda: client.get_album(browse_id)) or {}
    audio_id = album.get("audioPlaylistId")
    if not audio_id:
        raise ValueError("YouTube Music cannot save that candidate as an album")
    if not already_saved:
        client.rate_playlist(audio_id, "LIKE")
        artist = ", ".join(a.get("name", "") for a in (album.get("artists") or []) if a.get("name"))
        store.add_saved_album({
            "browse": browse_id, "title": album.get("title") or candidate["title"],
            "artist": artist or candidate.get("artist") or item.get("artist") or "",
            "year": album.get("year"), "type": album.get("type"),
            "thumbnail": best_thumb(album.get("thumbnails")) or candidate.get("thumbnail"),
        })
    source = item.get("source_uri") or "|".join((
        normalize(item.get("title")), normalize(item.get("artist"))))
    state = _load_state(store, _ALBUM_STATE)
    state.add(_fingerprint("album", source))
    _save_state(store, _ALBUM_STATE, state)
    return {"status": "skipped" if already_saved else "imported",
            "reason": "already_in_library" if already_saved else "saved_by_user",
            "browse_id": browse_id}
