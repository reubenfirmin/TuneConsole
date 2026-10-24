"""Publish complete, durable playlist backups before any destructive operation."""
import json
import os
import re
from uuid import uuid4

from yt_playlist.core import paths
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.matching import track_artist
from yt_playlist.util.retry import with_retry


def backup_playlist(store, playlist_id, now, client, *, snapshot=None) -> str:
    """Publish a durable, ordered remote snapshot before any playlist deletion.

    A caller that already verified a complete source may pass that exact snapshot, so the
    recovery artifact and the copy-before-delete gate describe the same remote contents.
    """
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    ytm = pl.ytm_playlist_id
    # central guard: every delete path backs up first, so refusing here keeps undeletable
    # system playlists (Liked Music, Episodes for Later) from ever being targeted.
    if ytm in SYSTEM_PLAYLIST_IDS:
        raise ValueError(f"“{pl.title}” is a system playlist and can't be deleted")
    detail = validate_playlist(with_retry(lambda: client.get_playlist(ytm, limit=None))
                               if snapshot is None else snapshot)
    tracks = [{"video_id": t["videoId"], "set_video_id": t.get("setVideoId"),
               "position": pos, "title": t.get("title"), "artist": track_artist(t),
               "duration_s": t.get("duration_seconds"), "remote": t}
              for pos, t in enumerate(detail["tracks"])]
    payload = {"playlist_id": playlist_id, "ytm_playlist_id": ytm, "identity_id": pl.identity_id,
               "title": detail.get("title") or pl.title, "tracks": tracks,
               "description": detail.get("description"), "captured_at": now,
               "snapshot_version": 2, "group": store.playlists.get_playlist_groups().get(ytm),
               "provenance": store.playlists.playlist_provenance(ytm)}
    # ytm comes from the YouTube API (untrusted); strip anything that could escape
    # the backups dir (e.g. "/" or "..") before using it in a filename.
    safe_ytm = re.sub(r"[^A-Za-z0-9_-]", "_", ytm)
    filename = f"{safe_ytm}_{playlist_id}_{int(now)}_{uuid4().hex}.json"
    backups = paths.backups_dir().resolve()
    path = (backups / filename).resolve()
    if path.parent != backups:  # belt-and-suspenders: must stay inside backups dir
        raise ValueError(f"refusing to write backup outside {backups}")
    # A failed write must never replace a previous recovery artifact or authorize deletion.
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(backups, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)

