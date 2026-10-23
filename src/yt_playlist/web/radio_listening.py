"""Observed listening, direction policy application and visible-menu attribution.

This owner has playback and policy ports, never a reference to the radio controller.
It updates the supplied session under its shared state lock; playlist transactions
and their receipt journal remain exclusively in RadioMutations.
"""
import json
import logging
import time
from uuid import uuid4

from yt_playlist.rec import measurement
from yt_playlist.web.radio_playback import PlaybackObservation, PlaybackRequest, PlaybackUnavailable
from yt_playlist.web.radio_playlist_io import PlaylistPending
from yt_playlist.web.radio_state import selected_slot

logger = logging.getLogger(__name__)


class RadioListening:
    def __init__(self, store, playback, policy, lock, now_fn):
        self.store, self.playback, self.policy = store, playback, policy
        self.lock, self.now_fn = lock, now_fn

    def current(self, s):
        if not s or s.stopped_by_user or (s.halted and not s.playback_confirmed):
            return False
        playing = self.playback.current()
        playlist = (playing.get("playlist_ytm_id") or "").removeprefix("RDAMPL")
        if s.playlist and playlist == s.playlist:
            s.launch_deadline = 0
            s.playback_confirmed = True
            return True
        owned = {t["video_id"] for t in s.tracks}
        if playlist and s.playback_confirmed and playing.get("video_id") not in owned:
            s.playback_confirmed = False
            return False
        return s.playback_confirmed or s.starting or time.monotonic() < s.launch_deadline

    def observe(self, s):
        # Refresh only a final saved future, never an intermediate replacement.
        refresh = s.refresh_needed and not s.requested_direction and (not s.replacement or s.replacement.saved)
        refresh_at = self.now_fn() if refresh else None
        try:
            observation = self.playback.observe(PlaybackRequest(
                s.playlist, tuple(t["video_id"] for t in s.tracks), refresh))
        except PlaybackUnavailable as exc:
            s.observer_error = str(exc)
            logger.debug("Radio observer reconnecting: %s", exc)
            raise PlaylistPending() from exc
        result = observation.state
        with self.lock:
            s.observer_error = None
            if not s.halted:
                s.error = None
            s.browser, s.browser_seen_at = result, observation.seen_at
            player = result.get("player") or {}
            if (player.get("playlist_ytm_id") == s.playlist and player.get("video_id")
                    and not player.get("paused")):
                s.playback_confirmed = True
                s.launch_deadline = 0
            # A request is not acknowledgement; late drift still needs repair.
            s.refresh_needed = not bool(result.get("playback_synced"))
            if refresh_at is not None and result.get("refresh") in {"clicked", "requested"}:
                s.edits.windows.append([refresh_at, self.now_fn() + 3])
            self.report(s, result)
        position = next((i for i, track in enumerate(s.tracks)
                         if player.get("playlist_ytm_id") == s.playlist
                         and track["video_id"] == player.get("video_id")), None)
        if position is not None and (position != s.position or s.observed_video != player.get("video_id")):
            self.record(s, position)
            self.update(s)
        elif position is None and player.get("playlist_ytm_id") == s.playlist:
            video = player.get("video_id")
            # Native playback can enter an owned song removed by a racing suffix edit.
            owned = {t["video_id"] for t in s.heard}
            owned.update(video for state in s.edits.recent_states for video in state["ids"])
            if video in owned and video in s.candidates.by_video:
                self.record(s, -1, track=s.candidates.by_video[video])
                self.update(s)

    @staticmethod
    def report(s, result):
        player, native = result.get("player") or {}, result.get("native") or {}
        video = player.get("video_id")

        def following(ids):
            index = ids.index(video) + 1 if video in ids else 0
            return tuple(ids[index:index + 3])

        report = (result.get("observerVersion"), player.get("playlist_ytm_id"), video,
                  following([t["video_id"] for t in s.tracks]), following(native.get("videoIds") or []),
                  following(player.get("loadedVideoIds") or []), result.get("playback_synced"),
                  result.get("refresh"), result.get("autoplay"), native.get("available"))
        refresh_state = result.get("refresh")
        if refresh_state in {"clicked", "requested", "waiting", "waiting for native playlist refresh"}:
            refresh_state = "pending"
        elif refresh_state in {"confirmed", "not requested"}:
            refresh_state = None
        signature = report[:7] + report[8:] + (refresh_state,)
        if signature == s.observer_report and result.get("refresh") not in {"clicked", "requested"}:
            return
        s.observer_report = signature
        logger.info("Radio playback: observer=%s playlist=%s current=%s saved_next=%s "
                    "native_next=%s embedded_next=%s synced=%s refresh=%s autoplay=%s native_available=%s",
                    *report)

    @staticmethod
    def player(s):
        if s.browser_seen_at is None:
            return None
        return PlaybackObservation(s.browser or {}, s.browser_seen_at).player(s.playlist, allow_paused=s.skip_requested)

    def record(self, s, position, *, track=None):
        track = track if track is not None else s.tracks[position]
        video, now = track["video_id"], self.now_fn()
        with self.lock:
            s.position, s.furthest = position, max(s.furthest, position)
            if s.directions:
                s.directions.retired = {t["video_id"] for t in s.tracks[:s.furthest + 1] + s.heard} | {video}
            if s.observed_video == video:
                return
            if s.observed_video:
                s.transition += 1
            s.observed_video = video
            s.starts[video] = now
            s.heard.append(track)
            choice = s.choices.pop(video, None)
        if choice:
            measurement.record_action(self.store, "start", now, keys=[track["key"]],
                event_id=f"radio:{s.episode_id}:{choice['menu_id']}:{choice['id']}")

    def update(self, s, *, force=False):
        with self.lock:
            self.policy.update_directions(s.candidate_view(), s.history_view(), force=force or s.feedback_dirty)
            s.feedback_dirty = False

    def feedback(self, s):
        update = self.policy.feedback(s.candidate_view(), s.feedback_context())
        s.accept_feedback(update)
        return update.changed

    def reject(self, s, track):
        s.rejected = set(self.policy.reject(s.candidate_view(), track))
        if s.directions:
            s.directions.blocked.add(track['video_id'])

    def seed_offer(self, candidates):
        tracks, menu_id = candidates.seed_offer(), uuid4().hex
        measurement.record_recommendations(self.store, 'radio_seed', tracks,
            self.now_fn(), request_id=menu_id, ranker='radio-seed',
            provenance={'menu_id': menu_id, 'fingerprint': candidates.fingerprint,
                        'choice_unit': 'radio_session'})
        self.store.settings.set_setting('radio_seed_offer', json.dumps({'tracks': tracks, 'menu_id': menu_id}))
        return tracks, menu_id

    def tune(self, s, candidates):
        """Rebuild the offered future without changing durable taste or observed listening."""
        metadata = list(s.tracks) + s.heard + [t for b in s.directions.slots if b for t in b['tracks']]
        if s.replacement:
            metadata += s.replacement.proposed
        candidates.retain(metadata)
        with self.lock:
            selected = selected_slot(s)
            choice = (s.requested_direction['choice'] if s.requested_direction else
                      s.replacement.choice_id if s.replacement else None)
            s.candidates = s.directions.candidates = candidates
            s.directions.blocked.difference_update(s.metadata_rejected - s.rejected)
            s.metadata_rejected.clear()
            s.directions.active = selected
            s.directions.update(s.heard or s.tracks[:1], [], s.transition, force=True)
            branch = s.directions.slots[selected]
            s.edits.queue({'slot': selected, 'tracks': list(branch['tracks']),
                           'choice': choice if branch['tracks'] else None})

    def select(self, s, proposal_id, menu_id):
        """Queue a validated visible choice; the observer settles saved writes later."""
        if not self.current(s) or not s.directions:
            return {'ok': False, 'reason': 'Start a Radio Session first.'}, False
        slot, branch = s.directions.find(proposal_id)
        if menu_id != s.directions.menu_id or branch is None:
            return {'ok': False, 'stale': True, 'reason': 'The next songs changed. Choose a direction again.'}, False
        if slot == selected_slot(s):
            return {'ok': True}, False
        if not branch['tracks']:
            return {'ok': False, 'reason': 'No more library songs are available in that direction.'}, False
        s.edits.queue({'slot': slot, 'tracks': list(branch['tracks']),
                       'choice': {'id': branch['id'], 'menu_id': menu_id}})
        s.error = None
        return {'ok': True}, True

    def seen(self, s, menu_id):
        """Return response fields and bounded enrichment work for a newly visible menu."""
        if not s or not s.directions or menu_id != s.directions.menu_id:
            return {"ok": False, "stale": True}, ()
        if menu_id in s.seen_menus:
            return {"ok": True}, ()
        branches = s.directions.public([])
        items = [{**track, "lane": branch["id"], "serving_context": {
            "proposal_id": branch["id"], "slot": branch["slot"],
            "sequence_position": index + 1, "active": branch["active"]}}
            for branch in branches for index, track in enumerate(branch["tracks"])]
        measurement.record_recommendations(self.store, "radio_directions", items,
            self.now_fn(), request_id=menu_id, ranker="radio-session",
            provenance={"episode_id": s.episode_id, "menu_id": menu_id,
                "radio_policy": "saved-playlist-v1", "tuning": s.candidates.tuning,
                "revision": s.directions.revision, "fingerprint": s.candidates.fingerprint,
                "history": [t["key"] for t in s.heard], "choice_unit": "radio_session"})
        s.seen_menus.add(menu_id)
        keys = tuple(dict.fromkeys(t["key"] for t in self.policy.metadata_tracks(
            s.candidate_view(), s.history_view(), selected_slot(s))
            if not t.get("genre") or not t.get("year")))
        return {"ok": True}, keys

    def refresh_metadata(self, s):
        now = time.monotonic()
        if not s.seen_menus or now - s.metadata_checked_at < 6:
            return
        s.metadata_checked_at = now
        view, history = s.candidate_view(), s.history_view()
        rejected = self.policy.refresh_metadata(view, history, self.policy.metadata_tracks(view, history, selected_slot(s)))
        if rejected is not None:
            s.metadata_revision += 1
            s.metadata_rejected = set(rejected)
            if rejected - s.directions.blocked:
                s.directions.blocked.update(rejected)
                s.feedback_dirty = True
