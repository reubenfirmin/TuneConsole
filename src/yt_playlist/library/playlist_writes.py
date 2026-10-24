"""Bounded playlist additions shared by imports, mutations and recovery orchestration."""
import logging
from collections import Counter
from dataclasses import dataclass

from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.util.retry import with_retry
from yt_playlist.util.matching import track_artist
from yt_playlist.util.thumbnails import best_thumb

logger = logging.getLogger(__name__)


def retain_addition_snapshot(store, playlist_id, addition, now):
    """Mirror a complete reconciliation read without another fetch or positional writes."""
    if not addition.failure:
        return
    snapshot = addition.failure.snapshot
    if snapshot is None:
        known = store.tracks.track_ids_for_videos(addition.confirmed)
        ids = list(dict.fromkeys(store.playlists.get_playlist_track_ids(playlist_id) +
                                [known[v] for v in addition.confirmed if v in known]))
        store.playlists.set_playlist_tracks(playlist_id, ids)
        store.playlists.set_playlist_track_count(playlist_id, len(ids), now)
        return
    track_ids = []
    for track in snapshot["tracks"]:
        album = track.get("album") or {}
        track_ids.append(store.tracks.upsert_track(
            track["videoId"], track.get("title") or "", track_artist(track),
            album.get("name") if isinstance(album, dict) else album,
            track.get("duration_seconds"), thumbnail=best_thumb(track.get("thumbnails"))))
    track_ids = list(dict.fromkeys(track_ids))
    store.playlists.set_playlist_tracks(playlist_id, track_ids)
    store.playlists.set_playlist_track_count(playlist_id, len(track_ids), now)
    store.playlists.ensure_playlist_thumbnail(playlist_id)


class PlaylistAddUncertain(ValueError):
    """Confirmed additions are separate from rejected and still-unknown occurrences."""
    def __init__(self, playlist_id, confirmed, skipped, uncertain, *, snapshot=None):
        self.playlist_id = playlist_id
        self.confirmed = tuple(confirmed)
        self.skipped = tuple(skipped)
        self.uncertain = tuple(uncertain)
        self.unattempted = ()
        self.snapshot = snapshot
        super().__init__(f"YouTube has not confirmed {len(self.uncertain)} playlist addition(s) "
                         f"to {playlist_id} ({len(self.confirmed)} confirmed, "
                         f"{len(self.skipped)} rejected). Confirmed progress and recovery details "
                         "are retained in Action Log. Check the playlist before retrying.")


@dataclass
class AdditionOutcome:
    confirmed: tuple
    skipped: tuple
    failure: PlaylistAddUncertain | None = None

    @property
    def added(self):
        return len(self.confirmed)

    def diagnostics(self):
        return {"confirmed": list(self.confirmed), "rejected": list(self.skipped),
                "uncertain": list(self.failure.uncertain) if self.failure else [],
                "unattempted": list(self.failure.unattempted) if self.failure else []}

    def finish(self):
        """Call only after the caller has retained membership and its action/recovery record."""
        if self.failure:
            raise self.failure


def account_additions(writer, client, playlist_id, video_ids, **kwargs):
    """Let mutation boundaries save partial progress before propagating uncertainty."""
    ids = list(video_ids)
    try:
        _added, skipped = writer(client, playlist_id, ids, **kwargs)
    except PlaylistAddUncertain as exc:
        return AdditionOutcome(exc.confirmed, exc.skipped, exc)
    remaining = Counter(skipped)
    confirmed = []
    for video in ids:
        if remaining[video]:
            remaining[video] -= 1
        else:
            confirmed.append(video)
    return AdditionOutcome(tuple(confirmed), tuple(skipped))


def _read(client, playlist_id):
    detail = with_retry(lambda: client.get_playlist(playlist_id, limit=None))
    return validate_playlist(detail)


def add_items(client, ytm_playlist_id, video_ids, set_video_ids=None, *, prior_tracks=None):
    """Add occurrences; retry only confirmed rejections after complete reconciliation.

    A known empty baseline can be supplied immediately after creating a playlist, before its
    first readable page exists. Existing destinations must be read before the first write.
    Unknown outcomes raise instead of letting callers publish an optimistic membership guess.
    """
    ids = list(video_ids)
    return _add_batches(client, ytm_playlist_id, ids, max(1, len(ids)), set_video_ids, prior_tracks)


def _add_batches(client, playlist_id, ids, batch_size, handles, prior_tracks):
    if not ids:
        return 0, []
    tracks = _read(client, playlist_id)["tracks"] if prior_tracks is None else prior_tracks
    counts = Counter(t["videoId"] for t in tracks)
    confirmed, skipped = [], []

    def add(batch, *, fallback):
        nonlocal counts
        error = None
        try:
            # Preserve repeated occurrences rather than relying on YouTube's default deduping.
            duplicate = any(counts[v] for v in batch) or len(set(batch)) != len(batch)
            result = client.add_playlist_items(playlist_id, batch, **({"duplicates": True} if duplicate else {}))
            status = result.get("status") if isinstance(result, dict) else result
        except Exception as exc:
            error, status, result = exc, None, None
        if status == "STATUS_SUCCEEDED":
            confirmed.extend(batch)
            counts.update(batch)
            _remember_added_handles(result, handles)
            return

        # A complete read can prove delivery. Absence cannot prove rejection: YouTube reads can
        # lag a committed write, so never replay an unacknowledged transport/unknown response.
        try:
            snapshot = _read(client, playlist_id)
            current = snapshot["tracks"]
        except Exception as exc:
            raise PlaylistAddUncertain(playlist_id, confirmed, skipped, batch) from exc
        available = Counter(t["videoId"] for t in current) - counts
        missing, delivered = [], []
        for video in batch:
            if available[video]:
                delivered.append(video)
                available[video] -= 1
            else:
                missing.append(video)
        confirmed.extend(delivered)
        counts.update(delivered)
        if handles is not None:
            # The new occurrence is appended after any pre-existing occurrences of this video.
            for row in current:
                if row["videoId"] in delivered and row.get("setVideoId"):
                    handles[row["videoId"]] = row["setVideoId"]
        if not missing:
            return
        if status != "STATUS_FAILED":
            raise PlaylistAddUncertain(playlist_id, confirmed, skipped, missing,
                                       snapshot=snapshot) from error
        if fallback:
            logger.warning("rejected batch for %s; retrying %d missing item(s)", playlist_id, len(missing))
            for video in missing:
                add([video], fallback=False)
        else:
            skipped.extend(missing)

    try:
        for start in range(0, len(ids), batch_size):
            add(ids[start:start + batch_size], fallback=True)
    except PlaylistAddUncertain as exc:
        remaining = Counter(ids) - Counter(confirmed) - Counter(skipped) - Counter(exc.uncertain)
        unattempted = []
        for video in ids:
            if remaining[video]:
                unattempted.append(video)
                remaining[video] -= 1
        exc.unattempted = tuple(unattempted)
        raise
    return len(confirmed), skipped


def _remember_added_handles(result, handles):
    if handles is None or not isinstance(result, dict):
        return
    for item in result.get("playlistEditResults", []):
        if item and item.get("videoId") and item.get("setVideoId"):
            handles[item["videoId"]] = item["setVideoId"]


def add_items_resilient(client, playlist_id, item_ids, batch_size=100, *, set_video_ids=None,
                        prior_tracks=None):
    """Bounded batches with per-item recovery for a rejected batch."""
    ids = list(item_ids)
    return _add_batches(client, playlist_id, ids, max(1, batch_size), set_video_ids, prior_tracks)
