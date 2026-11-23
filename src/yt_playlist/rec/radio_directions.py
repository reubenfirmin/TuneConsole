"""Four stable, session-scoped continuations. No playlist or database writes."""
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from uuid import uuid4

from yt_playlist.util import genre_map
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec.radio_genres import GenrePolicy, genre
from yt_playlist.rec.radio_restored import RestoredCandidates

DEPTH = 6
DECAY_TRANSITIONS = 3
INITIAL_DECAY_TRANSITIONS = (5, 4, 3)


def decay_progress(branch, deadline, transition):
    if deadline is None:
        return {"decay_remaining": DECAY_TRANSITIONS, "decay_total": DECAY_TRANSITIONS}
    return {"decay_remaining": max(0, deadline - transition),
            "decay_total": max(DECAY_TRANSITIONS, deadline - branch.get("born", transition))}


class RadioDirections:
    def __init__(self, candidates, seed):
        self.candidates = candidates
        self.active = 0
        self.slots = [None] * 4
        self.seed = seed
        self.blocked = set()
        self.retired = set()
        self.continuation_excluded = set()
        self.feedback = {}
        self.revision = 0
        self.menu_id = uuid4().hex
        self.context = None
        self.decay = {}
        self.transition = 0
        self.filling = False
        self.energy_start = None

    def snapshot(self):
        """Detach mutable menu state without copying the shared candidate model."""
        return deepcopy(self, {id(self.candidates): self.candidates})

    def _allowed(self, track):
        return track["video_id"] not in self.blocked

    def _genres(self, history, *, current=None):
        theme = getattr(self.candidates, 'tuning', {}).get('theme', .5)
        return GenrePolicy(theme, history, current=current, axes=getattr(self.candidates, 'axes', None))

    def mix_position(self, history):
        if self.energy_start is not None:
            return max(0, len(history) - self.energy_start)
        if getattr(self.candidates, 'tuning', {}).get('energy', .5) != .5:
            return max(0, len(history) - 1)
        return None

    def continue_sequence(self, history, prefix, anchor, excluded, *, pool=None, depth=DEPTH, reserved=(),
                          picker=None):
        """Extend an unsent branch using the current candidate and feedback policy."""
        current = history[-1] if history else anchor
        genres = self._genres(history, current=current)
        proposal = [t for t in prefix if genres.allows(t)]
        intensity_active = self.mix_position(history) is not None
        destination = proposal[-1] if proposal and intensity_active else anchor
        excluded = set(excluded) | self.blocked | self.retired
        while len(proposal) < depth:
            sequence = history + proposal
            position = self.mix_position(sequence)
            if picker is not None:
                pick = picker(sequence, destination, self.feedback, excluded, current, position, reserved)
            elif pool is None:
                pick = self.candidates.next_track(sequence, target=destination,
                    feedback=self.feedback, excluded=excluded, current=current,
                    destination=[(t['key'], t['video_id']) for t in sequence],
                    recipe={'surface': 'radio', 'tuning': dict(getattr(self.candidates, 'tuning', {}))}, policy='upload',
                    mix_position=position, reserved=reserved)
            else:
                pick = self.candidates.next_in_pool(pool, sequence, destination, self.feedback, excluded,
                                                    include_fresh=True, mix_position=position)
            if pick is None:
                break
            proposal.append(pick)
            if intensity_active:
                destination = pick
        return proposal

    def _label(self, tracks, anchor):
        axes = getattr(self.candidates, 'axes', None)
        families = Counter(axes.get(t.get('genre'), (None, None))[0] if axes is not None
                           else genre_map.family(t.get('genre')) for t in tracks)
        families.pop(None, None)
        families.pop("", None)
        family = families.most_common(1)[0][0] if families else None
        artists = list(dict.fromkeys(t.get("artist") for t in tracks if t.get("artist")))
        return (family or anchor.get("genre") or anchor.get("artist") or "Keep exploring",
                " · ".join(artists[:2]))

    @work_span("radio: plan local directions")
    def update(self, history, scheduled, transition, *, force=False, depth=DEPTH):
        """Advance the active plan; age alternatives on song changes, never on polling."""
        candidates = (self.candidates.planning_model() if isinstance(self.candidates, RestoredCandidates)
                      else self.candidates)
        prepared = getattr(candidates, 'prepared', None)
        with prepared.planning_read() if prepared is not None else nullcontext():
            return self._update(history, scheduled, transition, force=force, depth=depth)

    def _update(self, history, scheduled, transition, *, force=False, depth=DEPTH):
        context = (tuple(t["video_id"] for t in history[-8:]),
                   tuple(t["video_id"] for t in scheduled), transition, self.active,
                   frozenset(self.blocked), frozenset(self.retired), genre(history[-1]) if history else "",
                   frozenset(self.continuation_excluded))
        if not force and context == self.context:
            return False
        self.transition = transition
        if force or not self.decay:
            unused = [slot for slot in range(4) if slot != self.active]
            self.decay = {slot: transition + takes
                          for slot, takes in zip(unused, INITIAL_DECAY_TRANSITIONS)}
        vacant = self.decay.pop(self.active, None)
        # Preserve the other deadlines when switching streams. A demoted active
        # stream takes the vacant beat; it does not reset all three timers.
        occupied = set(self.decay.values())
        for slot in range(4):
            if slot == self.active or slot in self.decay:
                continue
            deadline = vacant if vacant is not None and vacant > transition else transition + 1
            while deadline in occupied:
                deadline += 1
            self.decay[slot] = deadline
            occupied.add(deadline)
        expired = [slot for slot, deadline in self.decay.items() if deadline <= transition]
        refresh_slot = min(expired, key=lambda slot: self.decay[slot]) if expired else None
        changed = False
        played = {t["video_id"] for t in history}
        active = self.slots[self.active]
        anchor = active["anchor"] if active else self.seed
        genres = self._genres(history)
        if genres.pinned and history:
            anchor = history[-1]
        old = active["tracks"] if active else []
        position = self.mix_position(history)
        if depth == 1 and hasattr(self.candidates, 'tuned_heads'):
            excluded = played | self.blocked | self.retired | self.continuation_excluded
            heads = self.candidates.tuned_heads(history, anchor, excluded, self.feedback, mix_position=position)
            if heads is not None:
                for slot, track in zip([self.active, *(slot for slot in range(4) if slot != self.active)], heads):
                    self.slots[slot] = self._proposal(track, [track], transition)
                self.revision += 1
                self.menu_id = uuid4().hex
                self.context = context
                self.filling = True
                return True
        scheduled_ids = {t["video_id"] for t in scheduled}
        prefix = list(scheduled)
        if not force:
            unavailable = played | scheduled_ids | self.retired | self.continuation_excluded
            prefix += [t for t in old if t["video_id"] not in unavailable
                       and self._allowed(t)]
        reserved = {t["video_id"] for slot, branch in enumerate(self.slots)
                    if slot != self.active and branch and not force for t in branch["tracks"]}
        prepared = position is None and getattr(self.candidates, 'prepared_streams', False)
        proposal = prefix[:depth]
        excluded = self.continuation_excluded | reserved if position is None else self.continuation_excluded
        if prepared:
            self.candidates.refresh_exclusions()
            group = self.candidates.cluster_for(anchor)
            for nearby in self.candidates.continuation_groups(group):
                if len(proposal) >= depth:
                    break
                pool = self.candidates.stream_pool(nearby, history + proposal, excluded | self.blocked | self.retired)
                proposal = self.continue_sequence(history, proposal, anchor, excluded, pool=pool, depth=depth)
        proposal = self.continue_sequence(history, proposal, anchor, excluded, depth=depth,
                                          reserved=reserved if position is not None else ())
        if position is None and len(proposal) < depth:
            proposal = self.continue_sequence(history, proposal, anchor, self.continuation_excluded, depth=depth)
        if active is None or [t["video_id"] for t in old] != [t["video_id"] for t in proposal] or force:
            self.slots[self.active] = self._proposal(anchor, proposal, transition)
            changed = True
        used = played | self.retired | {t["video_id"] for t in proposal}
        anchors = [self.slots[self.active]['anchor']]

        def stale(slot, branch):
            return (branch is None or force or slot == refresh_slot
                    or genres.pinned and (not genres.allows(branch['anchor'])
                                          or any(not genres.allows(t) for t in branch['tracks']))
                    or any(t['video_id'] in played | self.retired or not self._allowed(t) for t in branch['tracks'])
                    or changed and any(t['video_id'] in used for t in branch['tracks']))

        alternatives = [slot for slot in range(4) if slot != self.active]
        pools, retained_anchors = {}, []
        if prepared:
            refill = [slot for slot in alternatives if stale(slot, self.slots[slot])]
            retained = [self.slots[slot] for slot in alternatives if slot not in refill]
            retained_anchors = [branch['anchor'] for branch in retained]
            if refill:
                excluded = used | self.blocked | {t['video_id'] for branch in retained for t in branch['tracks']}
                pools = dict(zip(refill, self.candidates.alternative_pools(history, anchors[0], retained_anchors,
                                                                           len(refill), excluded, depth)))
        for slot in alternatives:
            old = self.slots[slot]
            if stale(slot, old):
                reserved = {t["video_id"] for other, branch in enumerate(self.slots)
                            if other not in (slot, self.active) and branch and not force for t in branch["tracks"]}
                exclusions = used | reserved
                pool = pools.get(slot)
                target = None
                if pool is not None:
                    previous = list({t['video_id']: t for t in [*anchors, *retained_anchors]}.values())
                    target = self.candidates.next_in_pool(pool, history, None, self.feedback,
                        exclusions | self.blocked | {t['video_id'] for t in previous}, other_anchors=previous,
                        mix_position=position)
                if target is None:
                    pool = None
                    target = self.candidates.direction_anchor(history, anchors, excluded=exclusions | self.blocked,
                                                               feedback=self.feedback, mix_position=position)
                if target is None:
                    # A small library can share songs across alternatives; each
                    # individual continuation still contains no repeats.
                    exclusions = played | self.retired | {t["video_id"] for t in anchors}
                    target = self.candidates.direction_anchor(history, anchors,
                        excluded=exclusions | self.blocked, feedback=self.feedback, mix_position=position)
                if target is None:
                    tracks = []
                    target = anchor
                else:
                    tracks = self.continue_sequence(history, [target], target, exclusions, pool=pool, depth=depth)
                    if pool is not None and len(tracks) < depth:
                        tracks = self.continue_sequence(history, tracks, target, exclusions, depth=depth)
                    if len(tracks) < depth:
                        # An exclusive anchor need not have six exclusive tracks.
                        tracks = self.continue_sequence(history, tracks, target, (), depth=depth)
                self.slots[slot] = self._proposal(target, tracks, transition)
                if old is not None and not force:
                    occupied = {deadline for other, deadline in self.decay.items() if other != slot}
                    self.decay[slot] = max(deadline for deadline in
                        range(transition + 1, transition + DECAY_TRANSITIONS + 1) if deadline not in occupied)
                changed = True
            branch = self.slots[slot]
            anchors.append(branch["anchor"])
            used.update(t["video_id"] for t in branch["tracks"])
        if changed:
            self.revision += 1
            self.menu_id = uuid4().hex
        self.context = context
        self.filling = depth < DEPTH
        return changed

    def fill(self, history, *, selected):
        """Extend the published heads in place, preserving their selection identities."""
        prepared = getattr(self.candidates, 'prepared', None)
        with prepared.planning_read() if prepared is not None else nullcontext():
            intensity_active = self.mix_position(history) is not None
            picker = None
            if getattr(self.candidates, '_tuning_read', False):
                self.candidates.tuning_inputs(history[-8:], self.candidates.eligibility_at)
                picker = self.candidates.tuned_fill_picker(history, energy=intensity_active)
            used = {track['video_id'] for branch in self.slots if branch for track in branch['tracks']}
            played = {track['video_id'] for track in history} | self.retired
            for slot in (selected, *(slot for slot in range(4) if slot != selected)):
                branch = self.slots[slot]
                if branch is None:
                    continue
                prefix = branch['tracks']
                prefix = [track for track in prefix if track['video_id'] not in played | self.blocked]
                excluded = used - {track['video_id'] for track in prefix}
                reserved = ()
                if slot == self.active:
                    if intensity_active:
                        reserved, excluded = excluded, set()
                    excluded.update(self.continuation_excluded)
                    prefix = [track for track in prefix if track['video_id'] not in self.continuation_excluded]
                tracks = self.continue_sequence(history, prefix[:DEPTH], branch['anchor'], excluded,
                                                reserved=reserved, picker=picker)
                if picker is not None and slot != self.active and len(tracks) < DEPTH:
                    # A small pool can share songs across directions, as in
                    # ordinary menu planning. Each direction still avoids repeats.
                    tracks = self.continue_sequence(history, tracks, branch['anchor'], (), picker=picker)
                branch['tracks'] = tracks
                used.update(track['video_id'] for track in tracks)
            if picker is not None:
                self.candidates.finish_tuned_fill(track for branch in self.slots if branch for track in branch['tracks'])
        self.filling = False
        self.revision += 1

    def _proposal(self, anchor, tracks, transition):
        if tracks and self.mix_position(()) is not None:
            anchor = tracks[-1]
        label, artists = self._label(tracks, anchor)
        cluster = self.candidates.cluster_for(anchor)
        label = self.candidates.modes.get(cluster, {}).get("label") or label
        return {"id": uuid4().hex, "anchor": anchor, "tracks": tracks,
                "label": label, "artists": artists, "born": transition, "cluster_id": cluster}

    def find(self, proposal_id):
        return next(((slot, branch) for slot, branch in enumerate(self.slots)
                     if branch and branch["id"] == proposal_id), (None, None))

    @property
    def exposure_id(self):
        return f'{self.menu_id}:heads' if self.filling else self.menu_id

    def activate(self, proposal_id):
        slot, branch = self.find(proposal_id)
        if branch is not None:
            self.active = slot
        return branch

    def public(self, scheduled):
        saved = {t["video_id"] for t in scheduled}
        return [{"id": branch["id"], "slot": slot, "active": slot == self.active,
                 "cluster_id": branch.get("cluster_id"),
                 **decay_progress(branch, self.decay.get(slot), self.transition),
                 "label": branch["label"], "artists": branch["artists"], "filling": self.filling,
                 "tracks": [{**t, "saved": t["video_id"] in saved} for t in branch["tracks"]]}
                for slot, branch in enumerate(self.slots) if branch]
