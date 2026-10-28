"""Complete remote reads and atomic publication of confirmed playlist contents."""
from dataclasses import dataclass
import math
from typing import Literal

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.matching import recording_title, track_artist


class PlaylistChanged(ValueError):
    pass


class PlaylistPending(Exception):
    """A temporary readback condition, never a user-facing error."""


def duration_seconds(value):
    """Keep invalid provider durations out of the catalog and time display."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(seconds) if math.isfinite(seconds) and seconds >= 1 else None


def readback_durations(tracks, remote, *, previous=()):
    """Fill missing times from the exact confirmed upload, retaining known values."""
    rows = {row['videoId']: row for row in remote}
    known = {track['video_id']: track.get('duration') for track in previous}
    result = []
    for track in tracks:
        row = rows.get(track['video_id'], {})
        duration = (duration_seconds(track.get('duration'))
                    or duration_seconds(known.get(track['video_id']))
                    or duration_seconds(row.get('duration_seconds'))
                    or duration_seconds(parse_duration(row.get('duration'))))
        result.append({**track, 'duration': duration} if duration is not None or 'duration' in track else dict(track))
    return tuple(result)


@dataclass(frozen=True)
class PlaylistEdit:
    kind: Literal["add", "remove", "move"]
    after: tuple[str, ...]
    track: dict | None = None
    row: dict | None = None
    successor: dict | None = None


@dataclass
class PendingWrite:
    edit: PlaylistEdit
    started_at: float | None
    receipt: dict | None = None


def append_receipt(result):
    """Persist only the status and the one acknowledged row."""
    if not isinstance(result, dict):
        return {"status": result} if isinstance(result, str) else {}
    receipt = {"status": result.get("status")}
    rows = result.get("playlistEditResults")
    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
        for key in ("videoId", "setVideoId"):
            if isinstance(rows[0].get(key), str) and rows[0][key]:
                receipt[key] = rows[0][key]
    return receipt


def matches_substitution(requested, row, receipt):
    if receipt.get("status") == "STATUS_SUCCEEDED" and receipt.get("setVideoId"):
        # Music can acknowledge the requested video but read back another upload
        # with different credits. The playlist occurrence handle proves which
        # row we added; differing handles must not fall back to metadata matching.
        return row.get("setVideoId") == receipt["setVideoId"]
    if row.get('playlistVideoId') == requested['video_id']:
        return True
    artist = track_artist(row)
    duration = row.get("duration_seconds") or parse_duration(row.get("duration"))
    return bool(row.get("title") and artist and requested.get("title") and requested.get("artist")
        and recording_title(row["title"]) == recording_title(requested["title"])
        and " ".join(artist.casefold().split()) == " ".join(requested["artist"].casefold().split())
        and isinstance(duration, (int, float)) and isinstance(requested.get("duration"), (int, float))
        and duration > 0 and abs(duration - requested["duration"]) <= 2)


class RadioPlaylistIO:
    def __init__(self, playlists, *, paused=lambda: False):
        self.playlists = playlists
        self.paused = paused
        self._unpublished = set()

    def read(self, client, playlist: str, *, expected: list[str] | None = None) -> list[dict]:
        detail = client.get_playlist(playlist, limit=None)
        # Initial header counts can lag; exact accepted contents prove readiness.
        if (expected is not None and isinstance(detail, dict) and isinstance(detail.get("tracks"), list)
                and all(isinstance(t, dict) for t in detail["tracks"])
                and [t.get("videoId") for t in detail["tracks"]] == expected):
            detail = {**detail, "trackCount": len(expected)}
        elif expected is not None and (not isinstance(detail, dict) or type(detail.get("trackCount")) is not int):
            # Fewer rows without a total cannot distinguish an omitted song
            # from a page that has not finished propagating.
            raise PlaylistPending()
        try:
            return validate_playlist(detail)["tracks"]
        except ValueError as exc:
            raise PlaylistPending() from exc

    def publish(self, local_id: int, title: str, previous: list[dict], tracks: list[dict], now: float):
        if local_id is None:
            return
        if self.paused():
            self._unpublished.add(local_id)
            return
        prefix = ([t["video_id"] for t in previous]
                  if local_id not in self._unpublished and len(tracks) > len(previous)
                  and list(tracks[:len(previous)]) == previous else None)
        self.playlists.publish_confirmed_tracks(local_id, title, tracks, now, expected_prefix=prefix)
        self._unpublished.discard(local_id)

    @staticmethod
    def write(client, playlist: str, edit: PlaylistEdit):
        if edit.kind == "add":
            return client.add_playlist_items(playlist, [edit.track["video_id"]])
        if edit.kind == "remove":
            return client.remove_playlist_items(playlist, [edit.row])
        if edit.kind == "move":
            return client.edit_playlist(playlist, moveItem=(edit.row["setVideoId"], edit.successor["setVideoId"]))
        raise ValueError(f"Unknown playlist edit: {edit.kind}")
