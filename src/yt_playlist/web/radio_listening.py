"""Observed listening, direction policy application and visible-menu attribution.

This owner has playback and policy ports, never a reference to the radio controller.
It updates the supplied session under its shared state lock; playlist transactions
and their receipt journal remain exclusively in RadioMutations.
"""
import json
import logging
import time
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from yt_playlist.core.activity import WorkClosing, check_stopping, cancellable_work
from yt_playlist.rec import choice_evidence, choice_outcomes, home_cache, measurement, musical_character
from yt_playlist.rec.radio_candidates import POLICY_VERSION
from yt_playlist.rec.radio_restored import RestoredCandidates
from yt_playlist.web.radio_playback import PlaybackObservation, PlaybackRequest, PlaybackUnavailable
from yt_playlist.web.radio_playlist_io import PlaylistPending
from yt_playlist.web.radio_state import selected_slot
from yt_playlist.web.radio_recovery import native_position, observed_queue, playback_position

logger = logging.getLogger(__name__)


def _track_group_evidence(context, version):
    group = next((group for group in context.get('musical_groups', ()) if group.get('version') == version), None)
    if group:
        return {'group_id': group['group_id'], 'group_version': version,
                'group_generation': context['group_generation']}
    return {'mode_id': version} if version and version != 'local' else {}


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
            if s.seeded_launch and playing.get('paused') is not False:
                return s.playback_confirmed or time.monotonic() < s.launch_deadline
            s.launch_deadline = 0
            s.playback_confirmed = True
            return True
        owned = {t["video_id"] for t in s.tracks}
        if playlist and s.playback_confirmed and playing.get("video_id") not in owned:
            s.playback_confirmed = False
            return False
        return s.playback_confirmed or s.starting or time.monotonic() < s.launch_deadline

    def observe(self, s, *, planning_cancelled=None, refresh_confirmed=False):
        # Normally refresh the final future. An in-progress edit can also
        # refresh a confirmed readback when playback needs its next saved row.
        refresh = s.refresh_needed and not s.requested_direction and (
            refresh_confirmed or not s.replacement or s.replacement.saved)
        refresh_at = self.now_fn() if refresh else None
        try:
            observation = self.playback.observe(PlaybackRequest(
                s.playlist, tuple(t["video_id"] for t in observed_queue(s.tracks, s.heard)), refresh,
                self.removed_current(s)))
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
        if observation.player(s.playlist) is None:
            return
        video = player.get('video_id')
        position = self.position(s, video)
        if position is None:
            # A retired saved occurrence is its own listen even if the two
            # queues happen to differ only at the currently playing slot.
            if any(t['video_id'] == video for t in s.heard) or (
                    s.edits.previously_saved(video) and self.native_selected(s, result, video)):
                if s.position == -1 and s.observed_video == video:
                    return
                track = self.upload(s, video)
                if track is not None:
                    self.record(s, -1, track=track)
                    if not s.seeded_launch:
                        self.update(s, cancelled=planning_cancelled)
                    return
            position = native_position(s.tracks, s.heard, s.playlist, result)
            if position is not None:
                # Read exact-upload metadata; queue evidence establishes only the
                # occurrence. The saved playlist and mutation journal stay intact.
                track = self.upload(s, video)
                if track is not None:
                    track = {**track, '_radio_queue_video_id': s.tracks[position]['video_id']}
                    with self.lock:
                        s.candidates.retain([track])
                    self.record(s, position, track=track)
                    if not s.seeded_launch:
                        self.update(s, cancelled=planning_cancelled)
                    return
                position = None
        if position is not None and (position != s.position or s.observed_video != player.get("video_id")):
            self.record_player(s, position, player)
            if not s.seeded_launch:
                self.update(s, cancelled=planning_cancelled)

    @staticmethod
    def position(s, video):
        return playback_position(s.tracks, s.heard, video)

    @staticmethod
    def native_selected(s, result, video):
        native = result.get('native') or {}
        ids = native.get('videoIds') or []
        index = native.get('selectedIndex')
        return (native.get('available') and native.get('playlistIds') in (None, [], [s.playlist])
                and ids.count(video) == 1 and (index is None or ids.index(video) == index))

    def removed_current(self, s):
        """Anchor a heard song outside the saved queue after its played prefix."""
        if s.position < 0 and s.heard and 0 <= s.furthest < len(s.tracks):
            video = s.heard[-1]['video_id']
            if self.position(s, video) is None:
                return video, s.furthest
        return None

    def upload(self, s, video):
        track = s.candidates.by_video.get(video) or self.store.radio_catalog.details([video]).get(video)
        if track is not None:
            return track
        playing = self.playback.current()
        if (playing.get('video_id') != video or playing.get('playlist_ytm_id') != s.playlist
                or playing.get('metadata_verified') is not True or not playing.get('title')
                or getattr(self.store, 'activity_paused', False)):
            return None
        # Admit the exact observed upload as its own catalog entry. Matching
        # titles never establish that it is the saved upload's recording.
        self.store.tracks.observe_track(video, playing['title'], playing.get('artist') or '',
            thumbnail=playing.get('thumbnail'), now=self.now_fn())
        return self.store.radio_catalog.details([video]).get(video)

    def queue_player(self, s, player):
        """Locate native playback for saved-queue edits without changing its identity."""
        if not player:
            return None
        video = player.get('video_id')
        position = self.position(s, video)
        if position is None and self.removed_current(s) == (video, s.furthest):
            position = s.furthest
        return {**player, 'video_id': s.tracks[position]['video_id']} if position is not None else player

    def recovery_position(self, s, player):
        video = player['video_id']
        position = self.position(s, video)
        if position is not None:
            return position
        if self.removed_current(s) == (video, s.furthest):
            return s.furthest
        return native_position(s.tracks, s.heard, s.playlist, s.browser)

    def record_queue_position(self, s, player):
        """Record playback and return the saved position that precedes its future."""
        if not player:
            return None
        video = player.get('video_id')
        position = self.position(s, video)
        if position is not None:
            self.record_player(s, position, player)
        elif self.removed_current(s) == (video, s.furthest):
            position = s.furthest
        return position

    def record_player(self, s, position, player):
        video = player.get('video_id')
        track = next((t for t in reversed(s.heard) if t['video_id'] == video), s.tracks[position])
        # Also record a return to the saved upload, replacing an earlier native
        # occurrence mapping while retaining both actual listens in history.
        self.record(s, position, track={**track, '_radio_queue_video_id': s.tracks[position]['video_id']})

    def report(self, s, result):
        player, native = result.get("player") or {}, result.get("native") or {}
        video = player.get("video_id")

        def following(ids):
            index = ids.index(video) + 1 if video in ids else 0
            return tuple(ids[index:index + 3])

        saved_index = self.recovery_position(s, player) if video else None
        saved = [t["video_id"] for t in s.tracks]
        saved_next = saved[saved_index + 1:saved_index + 4] if saved_index is not None else []
        native_ids, native_index = native.get("videoIds") or [], native.get("selectedIndex")
        native_next = (tuple(native_ids[native_index + 1:native_index + 4])
                       if type(native_index) is int and native_index >= 0 else following(native_ids))
        report = (result.get("observerVersion"), player.get("playlist_ytm_id"), video,
                  saved_next, native_next,
                  following(player.get("loadedVideoIds") or []), result.get("playback_synced"),
                  result.get("refresh"), result.get("autoplay"), native.get("available"),
                  saved[saved_index] if saved_index is not None else None, saved_index,
                  native_index, native.get("selectedItem"), player.get("ended"), player.get("error"))
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
                    "native_next=%s embedded_next=%s synced=%s refresh=%s autoplay=%s native_available=%s "
                    "saved_current=%s saved_index=%s native_index=%s native_row=%s ended=%s error=%s",
                    *report)

    @staticmethod
    def player(s, *, allow_paused=False):
        if s.browser_seen_at is None:
            return None
        explicit_play = (s.requested_direction and s.requested_direction.get('play')
                         or s.replacement and s.replacement.play)
        return PlaybackObservation(s.browser or {}, s.browser_seen_at).player(
            s.playlist, allow_paused=bool(allow_paused or s.skip_requested or explicit_play))

    def record(self, s, position, *, track=None):
        track = track if track is not None else s.tracks[position]
        video, now = track["video_id"], self.now_fn()
        with self.lock:
            s.edits.observe_song(track.get('_radio_queue_video_id', video))
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
            action = f"radio:{s.episode_id}:{choice['menu_id']}:{choice['id']}"
            if choice.get('record_choice_on_start'):
                choice_evidence.safe_respond(self.store, choice['menu_id'], 'choice', s.started_at,
                    option_id=choice['option_id'], details={'episode_id': s.episode_id})
            measurement.record_action(self.store, "start", now, keys=[track["key"]],
                event_id=action)
            choice_outcomes.started(self.store, s.identity, s.playlist, video, choice['menu_id'],
                choice.get('option_id', choice['id']), now, ids=(f'{action}:start', f'{action}:experience'),
                details={'episode_id': s.episode_id, 'receipt_id': action})
        else:
            choice_outcomes.continue_playback(self.store, s.identity, s.playlist, video, now)

    def update(self, s, *, force=False, cancelled=None):
        # Planning reads the catalog and cooperatively yields to requests. Never
        # retain the session lock during that work: status and Stop need it too.
        with self.lock:
            if s.stop.is_set() or s.directions is None:
                return
            view, history = s.candidate_view(), s.history_view()
            source, dirty = s.directions, s.feedback_dirty
            if source.filling:
                previous = source.context[0] if source.context else ()
                if (view.candidates.tuning['theme'] != 1 or not history.heard
                        or previous and previous[-1] == history.heard[-1]['video_id']):
                    return
            requested, replacement = s.requested_direction, s.replacement
            before = source.snapshot()
            planned = before.snapshot()
        with cancellable_work(cancelled) if cancelled is not None else nullcontext():
            self.policy.update_directions(replace(view, directions=planned), history, force=force or dirty)
        with self.lock:
            if (s.stop.is_set() or cancelled is not None and cancelled()
                    or s.candidate_view() != view or s.history_view() != history
                    or s.requested_direction is not requested or s.replacement is not replacement
                    or s.feedback_dirty != dirty or vars(source) != vars(before)):
                return
            s.directions = planned
            s.feedback_dirty = False

    def initial_directions(self, s):
        """Plan an unpublished first menu without blocking status or Stop."""
        self.update(s, force=True)

    def feedback(self, s):
        update = self.policy.feedback(s.candidate_view(), s.feedback_context())
        s.accept_feedback(update)
        return update.changed

    def reject(self, s, track):
        s.rejected = set(self.policy.reject(s.candidate_view(), track))
        if s.directions:
            s.directions.blocked.add(track['video_id'])

    def seed_offer(self, candidates):
        tracks = candidates.seed_offer()
        if not tracks:
            return [], None
        menu_id = uuid4().hex
        tracks = [{**track, 'serving_context': {**track.get('serving_context', {}),
                   'ordering_ranker': POLICY_VERSION, **musical_character.provenance(track)}} for track in tracks]
        measurement.record_recommendations(self.store, 'radio_seed', tracks,
            self.now_fn(), request_id=menu_id, ranker='radio-seed',
            provenance={'menu_id': menu_id, 'fingerprint': candidates.fingerprint,
                        'choice_unit': 'radio_session'})
        if not getattr(self.store, 'activity_paused', False):
            self.store.settings.set_setting('radio_seed_offer', json.dumps({'tracks': tracks, 'menu_id': menu_id}))
        context = getattr(candidates, 'choice_context', None) or (home_cache.read(self.store, 'home_prepared') or {}).get("choice_context", {})
        choice_evidence.safe_issue(self.store, "radio", [{"id": t["video_id"], "position": i + 1,
            "tracks": [t], **_track_group_evidence(context, t.get('_radio_mode')), "intent": "seed",
            "score": getattr(candidates, "prior", {}).get(t.get("_radio_mode"), {}).get(t["key"])}
            for i, t in enumerate(tracks)], self.now_fn(), opportunity_id=menu_id,
            context={**context, "radio": {"fingerprint": candidates.fingerprint, "tuning": candidates.tuning,
                                        'ordering_ranker': POLICY_VERSION, 'musical_policy': musical_character.VERSION}})
        return tracks, menu_id

    def capture_menu(self, s):
        branches = s.directions.public([])
        if not branches:
            return
        menu_id = s.directions.exposure_id
        context = getattr(s.candidates, 'choice_context', None) or (home_cache.read(self.store, 'home_prepared') or {}).get("choice_context", {})
        choice_evidence.safe_issue(self.store, "radio", [{"id": branch["id"],
            "position": branch["slot"] + 1, "active": branch["active"], "tracks": branch["tracks"],
            **_track_group_evidence(context, branch.get('cluster_id') or (branch['tracks'] or [{}])[0].get('_radio_mode')),
            "intent": "direction"}
            for branch in branches], self.now_fn(), opportunity_id=menu_id, episode_id=s.episode_id, reuse=True,
            context={**context, "radio": {"fingerprint": s.candidates.fingerprint, "tuning": s.candidates.tuning,
                'ordering_ranker': POLICY_VERSION, 'musical_policy': musical_character.VERSION,
                "now": s.candidates.now_posterior, "now_sampled_at": s.candidates.now_sampled_at,
                "history": [t["key"] for t in s.heard]}})

    def tune(self, s, candidates, *, current=lambda: True, song=None):
        """Rebuild the offered future without changing durable taste or observed listening."""
        with self.lock:
            requested, replacement = s.requested_direction, s.replacement
        attempt = 0
        while True:
            with self.lock:
                if (not current() or s.stop.is_set() or s.directions is None
                        or s.requested_direction is not requested or s.replacement is not replacement):
                    return False
                view, history = s.candidate_view(), s.history_view()
                source, dirty = s.directions, s.feedback_dirty
                metadata_revision = s.metadata_revision
                before = source.snapshot()
                planned = before.snapshot()
                selected = selected_slot(s)
                choice = requested['choice'] if requested else replacement.choice_id if replacement else None
                requested_play = requested.get('play') if requested else None
                replacing_play = replacement.play if replacement else None
                keeps_song = (requested_play or replacing_play
                              or (requested and requested.get('resequence_after'))
                              or (replacement and replacement.resequence_after))
                energy_changed = candidates.tuning['energy'] != view.candidates.tuning['energy']
                sequencing_changed = energy_changed or candidates.tuning.get('journey') != view.candidates.tuning.get('journey')
                settled = (s.edits.handoff or song) if not keeps_song else None
                saved_song = None
                if settled:
                    # Confirmed provider substitutions carry the same choice receipt.
                    saved_song = next((track for track in history.tracks[history.furthest + 1:]
                                       if track['video_id'] == settled.video_id or
                                       (settled.choice and s.choices.get(track['video_id']) == settled.choice)), None)
                    choice = s.choices.get(saved_song['video_id']) if saved_song else None
            # Only private computation is cancellable. Its final check precedes
            # publication, so cancellation cannot skip an accepted plan's save.
            with (cancellable_work(lambda: not current() or s.stop.is_set()),
                  candidates.prepared.planning_read() if candidates.prepared is not None else nullcontext()):
                playing = deepcopy(list(history.heard or history.tracks[:1]))
                if energy_changed or planned.energy_start is None:
                    planned.energy_start = len(playing)
                if getattr(candidates, '_tuning_read', False):
                    candidates.tuning_inputs(playing[-8:], self.now_fn())
                else:
                    metadata = list(history.tracks + history.heard)
                    metadata += [t for b in planned.slots if b for t in [b['anchor'], *b['tracks']]]
                    if requested:
                        metadata += requested['tracks']
                    if replacement:
                        metadata += replacement.proposed
                    candidates.retain(deepcopy(metadata))
                    candidates.refresh_exclusions(self.now_fn())
                planned.candidates = candidates
                planned.blocked.difference_update(view.metadata_rejected - view.rejected)
                planned.active = selected
                planned.update(playing, [], history.transition, force=True, depth=1)
                if settled:
                    branch = planned.slots[selected]
                    prefix = [saved_song] if saved_song and saved_song['video_id'] not in planned.blocked else []
                    tracks = planned.continue_sequence(playing, prefix, branch['anchor'], settled.excluded, depth=1)
                    planned.slots[selected]['tracks'] = tracks
                    if not tracks or tracks[0] != saved_song:
                        choice = None
            with self.lock:
                check_stopping()
                if (not current() or s.stop.is_set() or s.requested_direction is not requested
                        or s.replacement is not replacement):
                    return False
                if (s.candidate_view() != view or s.history_view() != history
                        or s.feedback_dirty != dirty or s.metadata_revision != metadata_revision
                        or vars(source) != vars(before)):
                    attempt += 1
                    logger.info("Radio tuning rebase: attempt=%d", attempt)
                    continue
                s.candidates, s.directions = candidates, planned
                s.metadata_rejected.clear()
                # Keep the explicit handoff and let the observer replan its
                # continuation after any outstanding provider write settles.
                if sequencing_changed:
                    if requested_play:
                        requested['resequence_after'] = requested_play['video_id']
                    elif requested is None and replacing_play:
                        replacement.resequence_after = replacing_play['video_id']
                if not keeps_song:
                    tracks = planned.slots[selected]['tracks']
                    request = {'slot': selected, 'tracks': list(tracks), 'choice': choice if tracks else None}
                    if settled:
                        request['excluded'] = list(settled.excluded)
                        request['resequence_after'] = saved_song['video_id'] if saved_song else settled.video_id
                    s.edits.queue(request)
                return True

    def fill_tuned_streams(self, s, *, cancelled):
        """Fill a published tuning menu after the observer has serviced playback."""
        with self.lock:
            source = s.directions
            if source is None or not source.filling or cancelled():
                return
            history, selected, revision = s.history_view(), selected_slot(s), source.revision
            planned = source.snapshot()
        try:
            with cancellable_work(cancelled):
                if isinstance(planned.candidates, RestoredCandidates):
                    planned.candidates = planned.candidates.planning_model()
                planned.fill(deepcopy(list(history.heard or history.tracks[:1])), selected=selected)
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Radio stream fill failed', exc_info=True)
            planned = None
        with self.lock:
            if (cancelled() or s.directions is not source or source.revision != revision
                    or s.history_view() != history or selected_slot(s) != selected):
                return
            if planned is None:
                source.filling = False
            else:
                s.directions = planned

    def select(self, s, proposal_id, menu_id, video_id=None, *, intent=None):
        """Queue a validated visible choice; the observer settles saved writes later."""
        if not self.current(s) or not s.directions:
            return {'ok': False, 'reason': 'Start a Radio Session first.'}, False
        slot, branch = s.directions.find(proposal_id)
        if menu_id != s.directions.menu_id or branch is None:
            return {'ok': False, 'stale': True, 'reason': 'The next songs changed. Choose a direction again.'}, False
        if video_id is None and slot == selected_slot(s):
            return {'ok': True}, False
        if not branch['tracks']:
            return {'ok': False, 'reason': 'No more library songs are available in that direction.'}, False
        index = next((i for i, track in enumerate(branch['tracks']) if track['video_id'] == video_id), None)
        if video_id is not None and index is None:
            return {'ok': False, 'stale': True, 'reason': 'That song left the stream. Choose a current song.'}, False
        index = index or 0
        self.capture_menu(s)
        exposure_id = s.directions.exposure_id
        choice_evidence.safe_respond(self.store, exposure_id, "choice", self.now_fn(), option_id=branch["id"],
            details={"episode_id": s.episode_id, "slot": slot,
                     **({'video_id': video_id, 'sequence_position': index + 1} if video_id else {})})
        request = {'slot': slot, 'tracks': list(branch['tracks'][index:]),
                   'choice': {'id': branch['id'], 'menu_id': exposure_id}}
        if video_id is not None:
            request['play'] = {'video_id': video_id, 'intent': intent}
            request['excluded'] = [t['video_id'] for t in branch['tracks'][:index]]
        s.edits.handoff = None
        s.edits.queue(request)
        s.error = None
        s.selection_error = None
        return {'ok': True}, True

    def seen(self, s, menu_id):
        """Return response fields and bounded enrichment work for a newly visible menu."""
        if not s or not s.directions or menu_id != s.directions.menu_id:
            return {"ok": False, "stale": True}, ()
        branches = s.directions.public([])
        if not branches:
            return {"ok": True}, ()
        exposure_id = s.directions.exposure_id
        if exposure_id in s.seen_menus:
            return {"ok": True}, ()
        self.capture_menu(s)
        choice_evidence.safe_respond(self.store, exposure_id, "visible", self.now_fn(),
                                    visible=[branch["id"] for branch in branches])
        items = [{**track, "lane": branch["id"], "serving_context": {**track.get('serving_context', {}),
            'ordering_ranker': POLICY_VERSION, **musical_character.provenance(track),
            "proposal_id": branch["id"], "slot": branch["slot"],
            "sequence_position": index + 1, "active": branch["active"]}}
            for branch in branches for index, track in enumerate(branch["tracks"])]
        measurement.record_recommendations(self.store, "radio_directions", items,
            self.now_fn(), request_id=exposure_id, ranker="radio-session",
            provenance={"episode_id": s.episode_id, "menu_id": menu_id,
                "radio_policy": "saved-playlist-v1", "tuning": s.candidates.tuning,
                "revision": s.directions.revision, "fingerprint": s.candidates.fingerprint,
                "history": [t["key"] for t in s.heard], "choice_unit": "radio_session"})
        s.seen_menus.add(exposure_id)
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
