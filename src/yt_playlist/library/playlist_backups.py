"""Publish complete, durable playlist backups before any destructive operation."""
import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

from yt_playlist.core import paths
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.matching import track_artist
from yt_playlist.util.retry import with_retry


def _sync_directory(path):
    if os.name != "nt":
        directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def backup_playlist(store, playlist_id, now, client, *, snapshot=None) -> str:
    """Publish a durable, ordered remote snapshot before any playlist deletion.

    A caller that already verified a complete source may pass that exact snapshot, so the
    recovery artifact and the copy-before-delete gate describe the same remote contents.
    """
    pl = store.playlists.get_playlist(playlist_id)
    if pl is None:
        raise ValueError("playlist no longer exists")
    if pl.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
        raise ValueError(f"“{pl.title}” is a system playlist and can't be deleted")
    detail = validate_playlist(with_retry(lambda: client.get_playlist(pl.provider_playlist_id, limit=None))
                               if snapshot is None else snapshot)
    return _capture_backup(store,pl,now,detail)


def backup_remote_playlist(store,account_id,ytm,title,now,snapshot):
    """Retain a recovery target even when its local membership has been retired."""
    pl = next((item for item in store.playlists.get_playlists()
               if item.account_id==account_id and item.provider_playlist_id==ytm),None)
    if pl is None:
        pl = SimpleNamespace(id=None,account_id=account_id,provider_playlist_id=ytm,title=title or ytm)
    return _capture_backup(store,pl,now,validate_playlist(snapshot))


def _capture_backup(store,pl,now,detail):
    playlist_id = pl.id
    ytm = pl.provider_playlist_id
    # central guard: every delete path backs up first, so refusing here keeps undeletable
    # system playlists (Liked Music, Episodes for Later) from ever being targeted.
    if ytm in SYSTEM_PLAYLIST_IDS:
        raise ValueError(f"“{pl.title}” is a system playlist and can't be deleted")
    tracks = [{"video_id": t["videoId"], "set_video_id": t.get("setVideoId"),
               "position": pos, "title": t.get("title"), "artist": track_artist(t),
               "duration_s": t.get("duration_seconds"), "remote": t}
              for pos, t in enumerate(detail["tracks"])]
    # Backup-v2 is a saved external format. Its field names stay stable while
    # the local model and SQL use account/provider playlist vocabulary.
    payload = {"playlist_id": playlist_id, "ytm_playlist_id": ytm, "identity_id": pl.account_id,
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
    # mkdir does not durably publish its entry in the parent. Include every
    # ancestor, even on retries: an earlier failed sync may have left it present
    # without making it durable. Finish this before authorizing any deletion.
    for parent in reversed(backups.parents):
        _sync_directory(parent)
    # A failed write must never replace a previous recovery artifact or authorize deletion.
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(backups)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)

