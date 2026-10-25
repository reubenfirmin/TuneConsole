"""Session recommendation and feedback policy, with no playback or playlist writes."""
import logging
from copy import copy
from dataclasses import dataclass
from typing import Mapping

from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec.radio_candidates import RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_genres import GenrePolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateView:
    candidates: RadioCandidates
    directions: RadioDirections | None
    selected: tuple[dict, ...]
    rejected: frozenset[str]
    metadata_rejected: frozenset[str]


@dataclass(frozen=True)
class PlaybackHistory:
    tracks: tuple[dict, ...]
    heard: tuple[dict, ...]
    position: int
    furthest: int
    transition: int

    @property
    def current(self):
        return self.heard[-1] if self.heard else self.tracks[0] if self.tracks else None


@dataclass(frozen=True)
class FeedbackContext:
    playlist: str
    identity: object
    started_at: float
    starts: Mapping[str, float]
    observed_video: str | None
    mutation_windows: tuple[tuple[float, float], ...]
    seen: frozenset


@dataclass(frozen=True)
class FeedbackUpdate:
    blocked: frozenset[str]
    seen: frozenset
    strengths: dict
    changed: bool


class RadioPolicy:
    def __init__(self, store, now_fn):
        self.store, self.now_fn = store, now_fn

    @staticmethod
    def genre_mismatch(view: CandidateView, history: PlaybackHistory, track):
        if getattr(view.candidates, 'cluster_members', None):
            return False  # Indexed streams navigate styles; explicit exclusions are checked separately.
        return not GenrePolicy(view.candidates.tuning["theme"], [], current=history.current).allows(track)

    @staticmethod
    def pick(view: CandidateView):
        if view.directions:
            used = {t["video_id"] for t in view.selected} | view.directions.blocked | view.rejected
            active = view.directions.slots[view.directions.active]
            pick = next((t for t in active["tracks"] if t["video_id"] not in used), None)
        else:
            pick = view.candidates.next_track(list(view.selected), excluded=view.rejected)
        return pick

    @staticmethod
    def reject(view: CandidateView, track):
        """A failed delivery excludes this upload without adding taste feedback."""
        video = track["video_id"]
        logger.info("Radio skipping unavailable candidate: video=%s", video)
        return view.rejected | {video}

    @staticmethod
    def update_directions(view: CandidateView, history: PlaybackHistory, *, force=False):
        position = max(0, history.furthest)
        view.directions.retired = {t["video_id"] for t in history.tracks[:position + 1] + history.heard}
        view.directions.update(list(history.heard or history.tracks[:1]), list(history.tracks[position + 1:]),
                               history.transition, force=force)

    @staticmethod
    def metadata_tracks(view: CandidateView, history: PlaybackHistory, selected):
        end = min(len(history.tracks), max(1, history.furthest + 1, history.position + 1))
        tracks = list(reversed(history.heard[-8:])) + list(reversed(history.tracks[max(0, end - 8):end]))
        branches = sorted(enumerate(view.directions.slots), key=lambda item: item[0] != selected)
        tracks += [t for _, branch in branches if branch for t in branch["tracks"]]
        return tracks + list(history.tracks[end:])

    def refresh_metadata(self, view: CandidateView, history: PlaybackHistory, tracks):
        if not view.candidates.refresh_metadata(tracks):
            return None
        rejected = set(view.metadata_rejected)
        for branch in view.directions.slots:
            if branch:
                rejected.update(t["video_id"] for t in branch["tracks"]
                    if not self.genre_mismatch(view, history, t) and not view.candidates.fits(t,
                        history.current if view.candidates.tuning["theme"] == 1 else branch["anchor"]))
        return frozenset(rejected)

    def feedback(self, view: CandidateView, context: FeedbackContext) -> FeedbackUpdate:
        store, now = self.store, self.now_fn()
        directions = view.directions
        seen, strengths = set(context.seen), dict(directions.feedback)
        changed = False
        disliked = store.rec_model.disliked_identity_keys()
        muted = {a.casefold() for a in store.rec_model.muted_artists()}
        suppressed = store.rec_model.suppressed_keys("for_you", now)
        blocked = view.rejected | view.metadata_rejected | {t["video_id"] for t in view.candidates.by_video.values()
            if t["key"] in disliked | suppressed or (t.get("artist") or "").casefold() in muted}
        if directions.blocked != blocked:
            changed = True
        ratings = [(key, at, .45, "like") for key, at in store.rec_model.recent_liked_with_ts(limit=200)]
        ratings += [(key, at, -.7, "dislike") for key, at in store.rec_model.disliked_with_ts()]
        for key, at, strength, kind in ratings:
            token = (kind, key, at)
            if at >= context.started_at and key in view.candidates.by_key and token not in seen:
                seen.add(token)
                strengths[key] = strength
                changed = True
        for event in store.player_events.player_events_since(max(context.started_at, now - 45), kind="track_exit"):
            video, at = event["video_id"], event["at"]
            token = ("exit", video, at, event["position"], event["duration"])
            if (event["playlist_ytm_id"] != context.playlist or event["identity_id"] != context.identity
                    or video not in context.starts or at < context.starts[video] or video == context.observed_video
                    or token in seen):
                continue
            seen.add(token)
            if any(start <= at <= end for start, end in context.mutation_windows):
                continue
            if classify_exit(event["position"], event["duration"]) != "skip":
                continue
            key = view.candidates.by_video[video]["key"]
            strengths[key] = max(-.6, strengths.get(key, 0) - .18)
            changed = True
        return FeedbackUpdate(frozenset(blocked), frozenset(seen), strengths, changed)

    @staticmethod
    def invalid(view: CandidateView, history: PlaybackHistory, track):
        if (RadioPolicy.genre_mismatch(view, history, track) or track["video_id"] in view.directions.blocked
                or track["key"] not in view.candidates.by_key):
            return True
        negative = sum(-strength * max(0, (view.candidates.similarity(
            track, view.candidates.by_key[key]) - .55) / .45)
            for key, strength in view.directions.feedback.items()
            if strength < 0 and key in view.candidates.by_key)
        return negative >= .32

    @staticmethod
    def replenish(view: CandidateView, history: PlaybackHistory, proposed, slot):
        anchor = view.directions.slots[slot]["anchor"]
        valid = [t for t in proposed if t["video_id"] not in view.directions.blocked]
        return view.directions.continue_sequence(list(history.heard or history.tracks[:1]), valid, anchor, ())

    @staticmethod
    def upgrade_directions(view: CandidateView, history: PlaybackHistory, candidates, *, force=False):
        """Plan privately so a failed pool upgrade leaves the last menu intact."""
        metadata = list(history.tracks + history.heard) + [t for b in view.directions.slots if b for t in [b["anchor"], *b["tracks"]]]
        candidates.retain(metadata)
        directions = copy(view.directions)
        directions.candidates = candidates
        directions.decay = dict(view.directions.decay)
        directions.slots = [dict(branch) if slot == directions.active and branch else None
                            for slot, branch in enumerate(view.directions.slots)]
        active = directions.slots[directions.active]
        if active:
            mid = candidates.cluster_for(active["anchor"])
            active["cluster_id"] = mid
            active["label"] = candidates.modes.get(mid, {}).get("label") or active["label"]
        position = max(0, history.furthest)
        directions.retired = {t["video_id"] for t in history.tracks[:position + 1] + history.heard}
        directions.context = None
        directions.update(list(history.heard or history.tracks[:1]), list(history.tracks[position + 1:]),
                          history.transition, force=force)
        return directions
