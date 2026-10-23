"""Decide suffix edits from complete readback; never perform transport or policy work."""
import logging
from dataclasses import dataclass
from typing import Mapping

from yt_playlist.web.radio_playlist_io import PendingWrite, PlaylistChanged, PlaylistEdit, matches_substitution

log = logging.getLogger(__name__)
LOOKAHEAD = 1


@dataclass(frozen=True)
class TailView:
    tracks: tuple[dict, ...]
    position: int
    furthest: int
    blocked: frozenset[str]
    known: Mapping[str, dict]
    stale_states: tuple[tuple[str, ...], ...] = ()
    skip_requested: bool = False


@dataclass(frozen=True)
class Substitution:
    requested: dict
    row: dict


@dataclass(frozen=True)
class TailDecision:
    complete: bool = False
    rejected: dict | None = None
    edit: PlaylistEdit | None = None


class RadioPlaylistTail:
    def __init__(self, tracks, proposed, slot, *, choice_id=None):
        self.allowed = {tuple(t["video_id"] for t in tracks)}
        self.proposed = list(proposed)
        self.slot = slot
        self.choice_id = choice_id
        self.confirmed = next(iter(self.allowed))
        self.write: PendingWrite | None = None
        self.saved = False

    @property
    def waiting(self):
        return self.write.edit.after if self.write else None

    @property
    def receipt(self):
        return self.write.receipt if self.write else None

    @property
    def adding(self):
        return self.write.edit.track if self.write else None

    def clear_wait(self):
        self.write = None

    def accept(self, remote: list[dict], view: TailView) -> bool | Substitution:
        ids = tuple(row["videoId"] for row in remote)
        if ids in self.allowed:
            return True
        if ids in view.stale_states:
            return False
        if (self.waiting and self.waiting[:-1] == self.confirmed
                and ids[:-1] == self.confirmed and len(ids) == len(self.waiting)
                and ids[-1] not in self.confirmed):
            requested = self.adding or view.known.get(self.waiting[-1])
            if requested and matches_substitution(requested, remote[-1], self.receipt or {}):
                return Substitution(requested, remote[-1])
        history = tuple(t["video_id"] for t in view.tracks[:max(view.position, view.furthest) + 1])
        owned = {video for previous in self.allowed for video in previous}
        owned.update(t["video_id"] for t in self.proposed)
        if (ids[:len(history)] == history and len(ids) == len(set(ids))
                and set(ids) <= owned and all(video in view.known for video in ids)):
            log.info("Radio direction readback rebased: previous=%s actual=%s", self.confirmed, ids)
            self.allowed.add(ids)
            self.confirmed = ids
            self.clear_wait()
            return True
        log.warning("Radio playlist conflict: history=%s known_states=%s actual=%s", history, sorted(self.allowed), ids)
        raise PlaylistChanged("Saved playlist contains an unrecognized edit")

    def adopt(self, ids: tuple[str, ...], requested: dict, accepted: dict):
        self.proposed = [accepted if t["video_id"] == requested["video_id"] else t for t in self.proposed]
        self.allowed.add(ids)
        self.write = PendingWrite(PlaylistEdit('add', ids, track=accepted),
                                  self.write.started_at if self.write else None)

    def pending(self, ids: tuple[str, ...], now: float) -> TailDecision | None:
        """Resolve an uncertain write before permitting another edit."""
        if self.waiting is not None:
            if ids != self.waiting:
                if self.write.started_at is None:
                    self.write.started_at = now
                if (ids == self.confirmed and self.waiting[:-1] == self.confirmed
                        and now - self.write.started_at >= 30):
                    missing = next((t for t in self.proposed if t["video_id"] == self.waiting[-1]), None)
                    if missing:
                        return TailDecision(rejected=missing)
                return TailDecision()
            self.confirmed = self.waiting
            self.clear_wait()
        elif ids != self.confirmed:
            return TailDecision()
        return None

    def plan(self, remote: list[dict], view: TailView, player: dict | None) -> TailDecision:
        ids = [t["videoId"] for t in remote]
        if player is None or player.get("video_id") not in ids:
            return TailDecision()
        position = ids.index(player["video_id"])
        if position < view.furthest:
            return TailDecision()
        history = ids[:position + 1]
        proposed = [t for t in self.proposed if t["video_id"] not in history and t["video_id"] not in view.blocked]
        target = proposed[:LOOKAHEAD]
        desired = history + [t["video_id"] for t in target]
        if ids == desired:
            self.proposed = proposed
            return TailDecision(complete=True)
        missing = next((t for t in target if t["video_id"] not in ids), None)
        duration = player.get("duration") or view.tracks[position].get("duration")
        progress = player.get("position")
        if (not missing and not view.skip_requested
                and (not duration or progress is None or duration - progress < 8)):
            return TailDecision()
        extra = next((t for t in remote[position + 1:] if t["videoId"] not in desired), None)
        after = list(ids)
        if missing:
            after.append(missing["video_id"])
            edit = PlaylistEdit("add", tuple(after), track=missing)
        elif extra:
            if not extra.get("setVideoId"):
                return TailDecision()
            after.remove(extra["videoId"])
            edit = PlaylistEdit("remove", tuple(after), row=extra)
        else:
            index = next(i for i in range(position + 1, len(ids)) if ids[i] != desired[i])
            moved = next(t for t in remote if t["videoId"] == desired[index])
            successor = remote[index]
            if not moved.get("setVideoId") or not successor.get("setVideoId"):
                return TailDecision()
            after.remove(moved["videoId"])
            after.insert(index, moved["videoId"])
            edit = PlaylistEdit("move", tuple(after), row=moved, successor=successor)
        return TailDecision(edit=edit)

    def begin(self, edit: PlaylistEdit, now: float):
        if self.write is not None:
            raise ValueError('Resolve the pending playlist write before beginning another')
        self.allowed.add(edit.after)
        self.write = PendingWrite(edit, now)
