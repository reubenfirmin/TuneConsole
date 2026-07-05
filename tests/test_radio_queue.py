import random

import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import radio, rec_params


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema()
    return s


def _picker(seq):
    # Return the picks in order; None once exhausted. pick_next is monkeypatched to this.
    it = iter(seq)
    return lambda st, se, now: next(it, None)


def _pk(n):
    return {"key": "k" + n, "video_id": "v" + n, "artist": "a" + n, "title": "t" + n, "url": "u" + n}


def _state_picker(order):
    """A pick_next stub that is a pure function of the CURRENT exclusion set: the first pick in `order`
    whose key is not excluded (queue + dispatched_keys + primed, via the real `_exclusions`). Unlike a
    one-shot iterator, re-deriving a tail from an unchanged exclusion set reproduces the same picks (it
    mirrors the real pick_next's determinism w.r.t. the model), and a pick dropped by delete-rebuild
    becomes pickable again as soon as its key leaves the exclusion set."""
    def _pick(store, session, now):
        excl = radio._exclusions(session)
        for p in order:
            if p["key"] not in excl:
                return p
        return None
    return _pick


def test_start_seeds_depth_and_primes(monkeypatch, store):
    rec_params.set_param(store, "radio_seed_depth", 3)
    monkeypatch.setattr(radio, "pick_next", _picker([_pk("1"), _pk("2"), _pk("3"), _pk("4")]))
    s = radio.RadioSession()
    plan = radio.start_session(store, s, now=1.0)
    assert plan["seed_vids"] == ["v1", "v2", "v3"]      # exactly seed_depth picks committed
    assert plan["first"]["video_id"] == "v1"
    assert plan["primed"]["video_id"] == "v2"           # boundary re-sync target = queue[1]
    assert s.active is True and s.pos == 0
    assert [q["video_id"] for q in s.queue] == ["v1", "v2", "v3"]


def test_start_session_fails_open_when_unpickable(monkeypatch, store):
    rec_params.set_param(store, "radio_seed_depth", 3)
    monkeypatch.setattr(radio, "pick_next", _picker([]))
    s = radio.RadioSession()
    plan = radio.start_session(store, s, now=1.0)
    assert plan is None
    assert s.active is False


def test_on_play_advances_folds_head_and_tops_up(monkeypatch, store):
    rec_params.set_param(store, "radio_seed_depth", 3)
    # Deterministic state-based picker (a function of the exclusion set): 1..5 in preference order.
    order = [_pk("1"), _pk("2"), _pk("3"), _pk("4"), _pk("5")]
    monkeypatch.setattr(radio, "pick_next", _state_picker(order))
    s = radio.RadioSession()
    radio.start_session(store, s, now=1.0)              # queue=[1,2,3], applied not yet set
    s.applied_vids = ["v1", "v2", "v3"]                 # simulate the bridge having reconciled the seeds
    out = radio.on_play(store, s, "v2", now=2.0)
    assert s.pos == 1
    assert "k1" in s.dispatched_keys                    # v1 folded into played (no-repeat)
    # DELETE-REBUILD: 3 is dropped (queue[idx+1:]) then re-eligible, so the rebuild re-picks it first
    # (an undropped candidate re-picked identically), then fills the rest of depth=3 fresh with 4, 5.
    assert [q["video_id"] for q in s.queue] == ["v1", "v2", "v3", "v4", "v5"]
    assert out["desired_vids"] == ["v1", "v2", "v3", "v4", "v5"]   # membership changed -> reconcile
    assert out["prime"]["video_id"] == "v3"            # next-after-current


def test_on_play_unchanged_membership_returns_none(monkeypatch, store):
    rec_params.set_param(store, "radio_seed_depth", 3)
    # Same deterministic picker/order as above, continued one more play frame with no model change
    # (no skips recorded): the delete-rebuild re-derives the identical continuation, so desired_vids
    # comes back None even though the tail was fully dropped and rebuilt from scratch.
    order = [_pk("1"), _pk("2"), _pk("3"), _pk("4"), _pk("5")]
    monkeypatch.setattr(radio, "pick_next", _state_picker(order))
    s = radio.RadioSession()
    radio.start_session(store, s, now=1.0)              # queue=[1,2,3]
    s.applied_vids = ["v1", "v2", "v3"]
    first = radio.on_play(store, s, "v2", now=2.0)       # queue -> [1,2,3,4,5]
    assert first["desired_vids"] == ["v1", "v2", "v3", "v4", "v5"]
    # on_play no longer commits applied_vids itself (that's the bridge's job, post-reconcile), so it
    # is still the stale seed value here; simulate the bridge's successful-reconcile commit before the
    # next play frame checks the membership delta against it.
    assert s.applied_vids == ["v1", "v2", "v3"]
    s.applied_vids = first["desired_vids"]
    out = radio.on_play(store, s, "v3", now=3.0)         # advance to the (unchanged) next queued track
    assert s.pos == 2
    assert "k2" in s.dispatched_keys
    assert [q["video_id"] for q in s.queue] == ["v1", "v2", "v3", "v4", "v5"]   # same tail, re-derived
    assert out["desired_vids"] is None                  # no membership change -> no reconcile
    assert out["prime"]["video_id"] == "v4"


def test_on_play_rebuild_purges_skip_tainted_pick(monkeypatch, store):
    """The reactivity case delete-rebuild exists for: seed A,B,C where C shares A's artist; skip A;
    on_play(B) rebuilds the tail from the CURRENT model (which now carries A's skip penalty) and C -
    unplayed but already queued - must be purged, not grandfathered in.

    Real pick_next runs (scores + artist cap + skip_penalty); only the score source and track metadata
    are stubbed, exactly like test_radio_picker's pattern. radio_variety is pinned to 0 (see the fixture
    setup in the test body): this test asserts the EXACT pick order out of the pre-sampling ranking,
    which sampling is layered above, not a replacement for.

    Base scores (descending): A=1.0, B=0.9, C=0.8, D=0.79, E=0.75, F=0.6.
    Artists: A and C are "art1" (shared); B/D/E/F each have a distinct artist.
    Defaults used (rec_params registry): radio_skip_artist_penalty=0.5, radio_artist_cap=3,
    radio_seed_depth=3. modeinfo is stubbed to None so the mode term of skip_penalty is inert.

    Seed (start_session, depth 3, top-3 by score) -> queue = [A, B, C].
    record_skip(A) at now=T logs ("art1", None, T).
    on_play(B, now=T) folds A into dispatched_keys, drops the tail ([C]), reverses C's note_dispatch
    (art1 count 2 -> 1, A's own contribution), then rebuilds depth=3 from {A, B} excluded:
      - C: adjusted = 0.8 - skip_penalty("art1", None, ..., T) = 0.8 - (0.5 * decay_weight(0, ...))
             decay_weight(age=0, ...) == 1.0 (a fresh skip does not decay), so 0.8 - 0.5*1.0 = 0.3
      - D: adjusted = 0.79 - 0 = 0.79   (distinct artist, no penalty)
      - E: adjusted = 0.75 - 0 = 0.75
      - F: adjusted = 0.6  - 0 = 0.6
    Pick order by adjusted score: D(0.79) > E(0.75) > F(0.6) > C(0.3) -> tail rebuilds to [D, E, F];
    C's 0.3 never clears the bar within depth=3, so it is dropped for good (until purged from
    session.skips or out-scored again, neither of which happens in this session).
    """
    rec_params.set_param(store, "radio_seed_depth", 3)
    rec_params.set_param(store, "radio_variety", 0)   # pin the pre-sampling ranking this test asserts
    scores = {"kA": 1.0, "kB": 0.9, "kC": 0.8, "kD": 0.79, "kE": 0.75, "kF": 0.6}
    meta = {
        "kA": {"video_id": "vA", "artist": "art1", "title": "A"},
        "kB": {"video_id": "vB", "artist": "artB", "title": "B"},
        "kC": {"video_id": "vC", "artist": "art1", "title": "C"},   # shares A's artist
        "kD": {"video_id": "vD", "artist": "artD", "title": "D"},
        "kE": {"video_id": "vE", "artist": "artE", "title": "E"},
        "kF": {"video_id": "vF", "artist": "artF", "title": "F"},
    }
    vid_to_key = {m["video_id"]: k for k, m in meta.items()}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {k: meta[k] for k in keys if k in meta})
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: vid_to_key.get(vid))

    s = radio.RadioSession()
    radio.start_session(store, s, now=1.0)
    assert [q["key"] for q in s.queue] == ["kA", "kB", "kC"]
    assert s.artist_counts == {"art1": 2, "artB": 1}   # A and C both counted under the shared artist

    radio.record_skip(store, s, "vA", now=2.0)
    assert s.skips == [("art1", None, 2.0)]

    out = radio.on_play(store, s, "vB", now=2.0)                 # fresh skip: age 0 -> decay 1.0

    keys = [q["key"] for q in s.queue]
    assert "kC" not in keys                                       # C is gone from the queue
    assert "kC" not in s.dispatched_keys                          # dropped unplayed, not folded as played
    assert keys == ["kA", "kB", "kD", "kE", "kF"]                 # D took C's old slot right after B
    assert out["prime"]["video_id"] == "vD"
    assert s.artist_counts == {"art1": 1, "artB": 1, "artD": 1, "artE": 1, "artF": 1}   # C's drop reversed


def test_on_play_backward_nav_is_noop(monkeypatch, store):
    """A reported vid EARLIER than the current position is a backward jump (rewind / replay an already-
    played track), not a forward advance. Folding it would be wrong (nothing new played), and rebuilding
    the tail at that earlier index would delete-and-reshuffle everything between it and the current pos,
    destroying already-played history and future picks the user has not even reached. The correct
    response is the same inert "do not fight navigation" no-op as an unrecognized (foreign) vid: play A,
    then B (pos advances to 1, A folds into dispatched_keys), then a play frame reporting A again must
    leave pos, dispatched_keys, and the queue completely untouched."""
    rec_params.set_param(store, "radio_seed_depth", 3)
    order = [_pk("1"), _pk("2"), _pk("3"), _pk("4"), _pk("5")]
    monkeypatch.setattr(radio, "pick_next", _state_picker(order))
    s = radio.RadioSession()
    radio.start_session(store, s, now=1.0)              # queue=[1,2,3], pos=0 (A is current)
    s.applied_vids = ["v1", "v2", "v3"]
    radio.on_play(store, s, "v2", now=2.0)               # play B: advance pos to 1, fold k1 (A) played
    assert s.pos == 1 and "k1" in s.dispatched_keys
    s.applied_vids = [q["video_id"] for q in s.queue]    # simulate the bridge's post-reconcile commit
    queue_before = list(s.queue)
    dispatched_before = set(s.dispatched_keys)
    out = radio.on_play(store, s, "v1", now=3.0)          # backward: A reported again, EARLIER than pos
    assert out == {"desired_vids": None, "prime": None}
    assert s.pos == 1                                     # unchanged: still at B
    assert s.dispatched_keys == dispatched_before          # A stays dispatched; nothing un-folded
    assert s.queue == queue_before                         # queue untouched: no delete-rebuild happened


def test_on_play_foreign_vid_is_noop(store):
    s = radio.RadioSession(); s.active = True; s.queue = [_pk("1")]; s.pos = 0
    out = radio.on_play(store, s, "vFOREIGN", now=2.0)
    assert out == {"desired_vids": None, "prime": None} and s.pos == 0


def test_on_play_inactive_session_is_noop(store):
    s = radio.RadioSession(); s.active = False
    out = radio.on_play(store, s, "v1", now=2.0)
    assert out == {"desired_vids": None, "prime": None}


def test_playlist_watch_url_always_carries_list():
    assert radio.playlist_watch_url("vid1", "PLxyz") == \
        "https://music.youtube.com/watch?v=vid1&list=PLxyz"


def test_note_dispatch_marks_artist_cap_only():
    s = radio.RadioSession()
    radio.note_dispatch(s, {"key": "a", "video_id": "va", "artist": "art"})
    assert s.artist_counts == {"art": 1}
    assert "a" not in s.dispatched_keys      # not PLAYED yet, just committed to the queue


def test_force_topup_rebuilds_in_place_without_advance(monkeypatch, store):
    """The Populate-tail affordance: rebuild the unplayed tail at the CURRENT position (no fold, no
    advance): a recorded skip purges tainted queued picks on demand, mid-track. Same fixture and
    arithmetic as test_on_play_rebuild_purges_skip_tainted_pick (C adjusted 0.8 - 0.5*1.0 = 0.3 loses
    to D 0.79 / E 0.75 / F 0.6), but WITHOUT a play frame: pos stays 0, A stays current."""
    rec_params.set_param(store, "radio_seed_depth", 3)
    rec_params.set_param(store, "radio_variety", 0)   # pin the pre-sampling ranking this test asserts
    scores = {"kA": 1.0, "kB": 0.9, "kC": 0.8, "kD": 0.79, "kE": 0.75, "kF": 0.6}
    meta = {
        "kA": {"video_id": "vA", "artist": "art1", "title": "A"},
        "kB": {"video_id": "vB", "artist": "artB", "title": "B"},
        "kC": {"video_id": "vC", "artist": "art1", "title": "C"},
        "kD": {"video_id": "vD", "artist": "artD", "title": "D"},
        "kE": {"video_id": "vE", "artist": "artE", "title": "E"},
        "kF": {"video_id": "vF", "artist": "artF", "title": "F"},
    }
    vid_to_key = {m["video_id"]: k for k, m in meta.items()}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: {k: meta[k] for k in keys if k in meta})
    monkeypatch.setattr(store, "identity_key_for_video", lambda vid: vid_to_key.get(vid))

    s = radio.RadioSession()
    radio.start_session(store, s, now=1.0)
    assert [q["key"] for q in s.queue] == ["kA", "kB", "kC"]

    radio.record_skip(store, s, "vA", now=2.0)
    plan = radio.force_topup(store, s, now=2.0)          # fresh skip: age 0 -> decay 1.0

    assert s.pos == 0                                    # no advance: A is still the current track
    keys = [q["key"] for q in s.queue]
    # depth counts UNPLAYED tracks ahead of pos: pos 0 + 3 ahead = 4 entries. B is re-picked (the
    # stale-prime exclusion bug this test caught would have dropped it), C is purged (0.3 < E 0.75),
    # D and E fill the remaining slots by adjusted score.
    assert keys == ["kA", "kB", "kD", "kE"]
    assert "kC" not in keys and "kC" not in s.dispatched_keys   # purged unplayed, eligible again later
    assert plan["desired_vids"] == ["vA", "vB", "vD", "vE"]     # membership changed -> apply plan
    assert plan["prime"]["video_id"] == "vB"             # prime = queue[pos+1]


def test_force_topup_inert_when_inactive(store):
    s = radio.RadioSession()
    assert radio.force_topup(store, s, now=1.0) == {"desired_vids": None, "prime": None}


# --- #93 fix 2: cross-session freshness cooldown ---

def _meta(keys):
    return {k: {"video_id": "v" + k, "title": "T" + k, "artist": "art_" + k} for k in keys}


def _ident(store):
    return store.upsert_identity("main", "ref", None, True)


def test_recent_radio_keys_matches_any_of_the_three_radio_settings(store):
    rec_params.set_param(store, "radio_freshness_days", 3.0)
    store.set_setting("radio_playlist_a_ytm", "PLA")
    store.set_setting("radio_playlist_b_ytm", "PLB")
    store.set_setting("radio_playlist_ytm", "PLV2")
    ident = _ident(store)
    now = 10 * 86400.0
    store.record_play_event(ident, "kA", "vA", now - 1 * 86400.0, playlist_ytm_id="PLA")
    store.record_play_event(ident, "kB", "vB", now - 2 * 86400.0, playlist_ytm_id="PLB")
    store.record_play_event(ident, "kV2", "vV2", now - 0.5 * 86400.0, playlist_ytm_id="PLV2")
    store.record_play_event(ident, "kOld", "vOld", now - 10 * 86400.0, playlist_ytm_id="PLA")   # outside window
    store.record_play_event(ident, "kOther", "vOther", now - 0.1 * 86400.0, playlist_ytm_id="PLREAL")  # not radio
    assert radio._recent_radio_keys(store, now) == {"kA", "kB", "kV2"}


def test_recent_radio_keys_off_when_freshness_days_zero(store):
    rec_params.set_param(store, "radio_freshness_days", 0)
    store.set_setting("radio_playlist_ytm", "PLV2")
    ident = _ident(store)
    store.record_play_event(ident, "k", "v", 100.0, playlist_ytm_id="PLV2")
    assert radio._recent_radio_keys(store, 200.0) == set()


def test_recent_radio_keys_empty_when_no_radio_playlist_ever_created(store):
    rec_params.set_param(store, "radio_freshness_days", 3.0)
    assert radio._recent_radio_keys(store, 100.0) == set()


def test_start_session_populates_recent_radio_before_seeding_and_excludes_it(monkeypatch, store):
    rec_params.set_param(store, "radio_seed_depth", 1)
    monkeypatch.setattr(radio, "_recent_radio_keys", lambda st, now: {"k1"})
    monkeypatch.setattr(radio, "pick_next", _state_picker([_pk("1"), _pk("2")]))
    s = radio.RadioSession()
    plan = radio.start_session(store, s, now=1.0)
    assert s.recent_radio == {"k1"}
    assert plan["seed_vids"] == ["v2"]   # k1 excluded via the real _exclusions union


def test_start_dual_session_populates_recent_radio_before_seeding_and_excludes_it(monkeypatch, store):
    rec_params.set_param(store, "radio_deck_size", 2)   # spec floor is 2; deck_size=1 would clamp to it
    monkeypatch.setattr(radio, "_recent_radio_keys", lambda st, now: {"k1", "k2"})
    order = [_pk(str(i)) for i in range(1, 7)]   # k1..k6
    monkeypatch.setattr(radio, "pick_next", _state_picker(order))
    s = radio.RadioSession()
    plan = radio.start_dual_session(store, s, now=1.0)
    assert s.recent_radio == {"k1", "k2"}
    assert plan["live"]["vids"] == ["v3", "v4"]      # k1/k2 excluded via recent_radio
    assert plan["standby"]["vids"] == ["v5", "v6"]


def test_pick_next_fail_open_clears_cooldown_when_whole_catalog_is_in_it(monkeypatch, store):
    # A small catalog entirely inside the freshness cooldown must not make radio refuse to (re)start:
    # pick_next clears session.recent_radio and recomputes eligibility once, rather than returning None.
    rec_params.set_param(store, "radio_variety", 0)
    scores = {"a": 1.0, "b": 0.9}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    s.recent_radio = {"a", "b"}   # the entire tiny catalog is under cooldown
    pick = radio.pick_next(store, s, now=10.0)
    assert pick is not None and pick["key"] == "a"
    assert s.recent_radio == set()   # the guard cleared it


def test_pick_next_leaves_cooldown_alone_when_eligible_without_clearing_it(monkeypatch, store):
    # The guard only fires when eligibility comes back EMPTY; if some other candidate is still
    # pickable, the cooldown must be left standing (it should keep suppressing "a" on later picks).
    rec_params.set_param(store, "radio_variety", 0)
    scores = {"a": 1.0, "b": 0.9}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    s = radio.RadioSession(); s.active = True
    s.recent_radio = {"a"}
    pick = radio.pick_next(store, s, now=10.0)
    assert pick["key"] == "b"
    assert s.recent_radio == {"a"}   # untouched: b was still eligible, no starvation


# --- #93 fix 1: rank-decay sampling ---

def test_sample_ranked_variety_zero_never_draws_rng(monkeypatch):
    def _boom():
        raise AssertionError("_rng.random() must not be called at variety 0")
    monkeypatch.setattr(radio._rng, "random", _boom)
    ranked = [("a", 1.0), ("b", 0.9), ("c", 0.8)]
    assert radio._sample_ranked(ranked, 0) == "a"   # byte-equivalent to the old argmax


def test_sample_ranked_seeded_matches_hand_computed_draw(monkeypatch):
    ranked = [("a", 1.0), ("b", 0.9), ("c", 0.8), ("d", 0.7), ("e", 0.6)]
    variety = 0.5
    # Hand-computed expectation: P(rank i) ∝ variety**i, drawn via r = rng.random() * total.
    weights = [variety ** i for i in range(len(ranked))]
    total = sum(weights)
    r = random.Random(7).random() * total
    acc, expected = 0.0, None
    for (k, _adj), w in zip(ranked, weights):
        acc += w
        if r < acc:
            expected = k
            break
    monkeypatch.setattr(radio, "_rng", random.Random(7))
    assert radio._sample_ranked(ranked, variety) == expected


def test_sample_ranked_higher_variety_can_reach_a_lower_rank(monkeypatch):
    # A fixed rng draw near the top of [0,1) lands past rank 0's slice once variety is high enough,
    # but never at variety 0 (which never even consults it).
    class _FixedRng:
        def random(self):
            return 0.999999
    monkeypatch.setattr(radio, "_rng", _FixedRng())
    ranked = [("a", 1.0), ("b", 0.9)]
    assert radio._sample_ranked(ranked, 0) == "a"
    # weights [1, 0.9], total 1.9 ; r = 0.999999*1.9 ≈ 1.8999981, past rank0's [0,1) slice -> rank 1.
    assert radio._sample_ranked(ranked, 0.9) == "b"


def test_two_sessions_different_seeded_rng_diverge_on_seed_set(monkeypatch, store):
    # The owner's actual complaint: a fresh session replaying the identical seed tracks every time.
    # With radio_variety > 0, two sessions seeded off different rng streams pick different tracks.
    rec_params.set_param(store, "radio_variety", 0.6)
    rec_params.set_param(store, "radio_seed_depth", 3)
    scores = {chr(97 + i): 1.0 - i * 0.05 for i in range(15)}   # 'a'..'o', descending
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))

    monkeypatch.setattr(radio, "_rng", random.Random(1))
    s1 = radio.RadioSession()
    plan1 = radio.start_session(store, s1, now=10.0)

    monkeypatch.setattr(radio, "_rng", random.Random(2))
    s2 = radio.RadioSession()
    plan2 = radio.start_session(store, s2, now=10.0)

    assert plan1["seed_vids"] != plan2["seed_vids"]


def test_variety_zero_equals_old_argmax_on_a_fixture_where_sampling_would_differ(monkeypatch, store):
    # Same fixture as the seeded-draw test above, but variety=0: must reproduce the deterministic
    # top-ranked pick every time regardless of what the rng would otherwise have drawn.
    rec_params.set_param(store, "radio_variety", 0)
    scores = {"a": 1.0, "b": 0.9, "c": 0.8, "d": 0.7, "e": 0.6}
    monkeypatch.setattr(radio, "_score_map", lambda st, now: dict(scores))
    monkeypatch.setattr(radio, "_modeinfo", lambda st: None)
    monkeypatch.setattr(store, "tracks_by_keys", lambda keys: _meta(keys))
    # Rig the rng to a value that (at variety > 0) would draw a lower rank, to prove variety=0 never
    # even consults it.
    class _FixedRng:
        def random(self):
            return 0.999999
    monkeypatch.setattr(radio, "_rng", _FixedRng())
    s = radio.RadioSession(); s.active = True
    assert radio.pick_next(store, s, now=10.0)["key"] == "a"
