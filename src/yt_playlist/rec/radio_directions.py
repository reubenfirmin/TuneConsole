"""Four stable, session-scoped continuations. No playlist or database writes."""
from collections import Counter
from uuid import uuid4

from yt_playlist.util import genre_map
from yt_playlist.core.work_diagnostics import work_span

DEPTH = 6
DECAY_TRANSITIONS = 3


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

    def _allowed(self, track):
        return track["video_id"] not in self.blocked

    def _extend(self, history, prefix, anchor, excluded):
        proposal = list(prefix)
        while len(proposal) < DEPTH:
            pick = self.candidates.next_track(history + proposal, target=anchor,
                feedback=self.feedback, excluded=set(excluded) | self.blocked | self.retired)
            if pick is None:
                break
            proposal.append(pick)
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
                   frozenset(self.blocked), frozenset(self.retired))
        if not force and context == self.context:
            return False
        changed = False
        played = {t["video_id"] for t in history}
        active = self.slots[self.active]
        anchor = active["anchor"] if active else self.seed
        old = active["tracks"] if active else []
        scheduled_ids = {t["video_id"] for t in scheduled}
        prefix = list(scheduled)
        if not force:
            prefix += [t for t in old if t["video_id"] not in played | scheduled_ids | self.retired
                       and self._allowed(t)]
        proposal = self._extend(history, prefix[:DEPTH], anchor, ())
        if active is None or [t["video_id"] for t in old] != [t["video_id"] for t in proposal] or force:
            self.slots[self.active] = self._proposal(anchor, proposal, transition)
            changed = True
        used = played | self.retired | {t["video_id"] for t in proposal}
        anchors = [anchor]
        for slot in range(4):
            if slot == self.active:
                continue
            old = self.slots[slot]
            stale = (old is None or force or transition - old["born"] >= DECAY_TRANSITIONS
                     or any(t["video_id"] in played | self.retired or not self._allowed(t)
                            for t in old["tracks"])
                     or changed and any(t["video_id"] in used for t in old["tracks"]))
            if stale:
                target = self.candidates.direction_anchor(history, anchors, excluded=used | self.blocked,
                                                           feedback=self.feedback)
                exclusions = used
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
                    tracks = self._extend(history, [target], target, exclusions)
                self.slots[slot] = self._proposal(target, tracks, transition)
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
                 "label": branch["label"], "artists": branch["artists"],
                 "tracks": [{**t, "saved": t["video_id"] in saved} for t in branch["tracks"]]}
                for slot, branch in enumerate(self.slots) if branch]
