"""Four stable, session-scoped continuations. No playlist or database writes."""
from collections import Counter
from uuid import uuid4

from yt_playlist.util import genre_map
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec.radio_genres import GenrePolicy, genre

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
        self.feedback = {}
        self.revision = 0
        self.menu_id = uuid4().hex
        self.context = None
        self.decay = {}
        self.transition = 0

    def _allowed(self, track):
        return track["video_id"] not in self.blocked

    def _genres(self, history, *, current=None):
        theme = getattr(self.candidates, 'tuning', {}).get('theme', .5)
        if getattr(self.candidates, 'cluster_members', None):
            theme = .5  # Prepared style coordinates own navigation in indexed streams.
        return GenrePolicy(theme, history, current=current)

    def continue_sequence(self, history, prefix, anchor, excluded):
        """Extend an unsent branch using the current candidate and feedback policy."""
        current = history[-1] if history else anchor
        genres = self._genres(history, current=current)
        proposal = [t for t in prefix if genres.allows(t)]
        intensity_active = getattr(self.candidates, 'tuning', {}).get('energy', .5) != .5
        destination = proposal[-1] if proposal and intensity_active else anchor
        while len(proposal) < DEPTH:
            pick = self.candidates.next_track(history + proposal, target=destination,
                feedback=self.feedback, excluded=set(excluded) | self.blocked | self.retired, current=current)
            if pick is None:
                break
            proposal.append(pick)
            if intensity_active:
                destination = pick
        return proposal

    @staticmethod
    def _label(tracks, anchor):
        families = Counter(genre_map.family(t.get("genre")) for t in tracks)
        families.pop(None, None)
        families.pop("", None)
        family = families.most_common(1)[0][0] if families else None
        artists = list(dict.fromkeys(t.get("artist") for t in tracks if t.get("artist")))
        return (family or anchor.get("genre") or anchor.get("artist") or "Keep exploring",
                " · ".join(artists[:2]))

    @work_span("radio: plan local directions")
    def update(self, history, scheduled, transition, *, force=False):
        """Advance the active plan; age alternatives on song changes, never on polling."""
        context = (tuple(t["video_id"] for t in history[-8:]),
                   tuple(t["video_id"] for t in scheduled), transition, self.active,
                   frozenset(self.blocked), frozenset(self.retired), genre(history[-1]) if history else "")
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
        scheduled_ids = {t["video_id"] for t in scheduled}
        prefix = list(scheduled)
        if not force:
            prefix += [t for t in old if t["video_id"] not in played | scheduled_ids | self.retired
                       and self._allowed(t)]
        reserved = {t["video_id"] for slot, branch in enumerate(self.slots)
                    if slot != self.active and branch and not force for t in branch["tracks"]}
        proposal = self.continue_sequence(history, prefix[:DEPTH], anchor, reserved)
        if len(proposal) < DEPTH:
            proposal = self.continue_sequence(history, proposal, anchor, ())
        if active is None or [t["video_id"] for t in old] != [t["video_id"] for t in proposal] or force:
            self.slots[self.active] = self._proposal(anchor, proposal, transition)
            changed = True
        used = played | self.retired | {t["video_id"] for t in proposal}
        anchors = [self.slots[self.active]['anchor']]
        for slot in range(4):
            if slot == self.active:
                continue
            old = self.slots[slot]
            stale = (old is None or force or slot == refresh_slot
                     or genres.pinned and (not genres.allows(old["anchor"])
                                           or any(not genres.allows(t) for t in old["tracks"]))
                     or any(t["video_id"] in played | self.retired or not self._allowed(t)
                            for t in old["tracks"])
                     or changed and any(t["video_id"] in used for t in old["tracks"]))
            if stale:
                reserved = {t["video_id"] for other, branch in enumerate(self.slots)
                            if other not in (slot, self.active) and branch and not force for t in branch["tracks"]}
                target = self.candidates.direction_anchor(history, anchors, excluded=used | self.blocked | reserved,
                                                           feedback=self.feedback)
                exclusions = used | reserved
                if target is None:
                    # A small library can share songs across alternatives; each
                    # individual continuation still contains no repeats.
                    exclusions = played | self.retired | {t["video_id"] for t in anchors}
                    target = self.candidates.direction_anchor(history, anchors,
                        excluded=exclusions | self.blocked, feedback=self.feedback)
                if target is None:
                    tracks = []
                    target = anchor
                else:
                    tracks = self.continue_sequence(history, [target], target, exclusions)
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
        return changed

    def _proposal(self, anchor, tracks, transition):
        if tracks and getattr(self.candidates, 'tuning', {}).get('energy', .5) != .5:
            anchor = tracks[-1]
        label, artists = self._label(tracks, anchor)
        cluster = self.candidates.cluster_for(anchor)
        label = self.candidates.modes.get(cluster, {}).get("label") or label
        return {"id": uuid4().hex, "anchor": anchor, "tracks": tracks,
                "label": label, "artists": artists, "born": transition, "cluster_id": cluster}

    def find(self, proposal_id):
        return next(((slot, branch) for slot, branch in enumerate(self.slots)
                     if branch and branch["id"] == proposal_id), (None, None))

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
                 "label": branch["label"], "artists": branch["artists"],
                 "tracks": [{**t, "saved": t["video_id"] in saved} for t in branch["tracks"]]}
                for slot, branch in enumerate(self.slots) if branch]
