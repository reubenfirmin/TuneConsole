"""Complete remote reads and atomic publication of confirmed playlist contents."""
from dataclasses import dataclass
from typing import Literal

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.matching import recording_title, track_artist


class PlaylistChanged(ValueError):
    pass


class PlaylistPending(Exception):
    """A temporary readback condition, never a user-facing error."""


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
    if (receipt.get("status") == "STATUS_SUCCEEDED" and receipt.get("videoId") == row["videoId"]
            and receipt.get("setVideoId") and row.get("setVideoId") == receipt["setVideoId"]):
        return True
    artist = track_artist(row)
    duration = row.get("duration_seconds") or parse_duration(row.get("duration"))
    return bool(row.get("title") and artist and requested.get("title") and requested.get("artist")
        and recording_title(row["title"]) == recording_title(requested["title"])
        and " ".join(artist.casefold().split()) == " ".join(requested["artist"].casefold().split())
        and isinstance(duration, (int, float)) and isinstance(requested.get("duration"), (int, float))
        and duration > 0 and abs(duration - requested["duration"]) <= 2)


class RadioPlaylistIO:
    def __init__(self, playlists):
        self.playlists = playlists

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
        prefix = ([t["video_id"] for t in previous]
                  if len(tracks) > len(previous) and list(tracks[:len(previous)]) == previous else None)
        self.playlists.publish_confirmed_tracks(local_id, title, tracks, now, expected_prefix=prefix)

    @staticmethod
    def write(client, playlist: str, edit: PlaylistEdit):
        if edit.kind == "add":
            return client.add_playlist_items(playlist, [edit.track["video_id"]])
        if edit.kind == "remove":
            return client.remove_playlist_items(playlist, [edit.row])
        if edit.kind == "move":
            return client.edit_playlist(playlist, moveItem=(edit.row["setVideoId"], edit.successor["setVideoId"]))
        raise ValueError(f"Unknown playlist edit: {edit.kind}")
