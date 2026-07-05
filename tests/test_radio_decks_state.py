import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import radio, rec_params


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema()
    return s


def _picker(seq):
    it = iter(seq)
    return lambda st, se, now: next(it, None)


def _pk(n):
    return {"key": "k" + n, "video_id": "v" + n, "artist": "a" + n, "title": "t" + n, "url": "u" + n}


def test_start_dual_seeds_two_disjoint_decks(monkeypatch, store):
    rec_params.set_param(store, "radio_deck_size", 3)
    # picker yields 1..6: deck A = 1,2,3 ; deck B = 4,5,6 (disjoint, session exclusion set enforced).
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, 7)]))
    s = radio.RadioSession()
    plan = radio.start_dual_session(store, s, now=1.0)
    assert plan["live"]["vids"] == ["v1", "v2", "v3"]
    assert plan["standby"]["vids"] == ["v4", "v5", "v6"]
    assert plan["live"]["boundary"] == "v3"     # arm the toggle at deck A's last track
    assert plan["standby"]["boundary"] == "v6"
    assert plan["live"]["first"]["video_id"] == "v1"
    assert s.active and s.dual_deck and s.live_label == "A" and s.pos == 0
    assert s.decks["B"]["boundary_vid"] == "v6"


def test_start_dual_none_when_nothing_pickable(monkeypatch, store):
    monkeypatch.setattr(radio, "pick_next", _picker([]))
    s = radio.RadioSession()
    assert radio.start_dual_session(store, s, now=1.0) is None
    assert s.active is False and s.dual_deck is False


def test_start_dual_none_when_deck_b_seeds_empty(monkeypatch, store):
    """Deck A seeds fully (3 picks) but the picker then exhausts, so deck B gets 0: shipping dual_deck
    with an empty standby is a gap (T7c review), not a partial success. This must fail open exactly like
    the deck-A-empty case: fully unwind (both decks back to empty, session inactive) and return None, so
    the bridge's `dplan is None` check (T7h) falls back to the v2 single-tab start_session path."""
    rec_params.set_param(store, "radio_deck_size", 3)
    # Exactly 3 picks total: enough for deck A (1,2,3), nothing left for deck B.
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, 4)]))
    s = radio.RadioSession()
    plan = radio.start_dual_session(store, s, now=1.0)
    assert plan is None
    assert s.active is False and s.dual_deck is False
    assert s.decks["A"]["queue"] == [] and s.decks["B"]["queue"] == []


def test_reset_initializes_decks():
    s = radio.RadioSession()
    assert s.dual_deck is False and s.live_label == "A" and s.epoch == 0
    assert set(s.decks) == {"A", "B"}
    assert s.decks["A"] == {"playlist_ytm": None, "queue": [], "applied_vids": [], "boundary_vid": None}
    assert s.live is s.decks["A"] and s.standby is s.decks["B"]


def test_live_standby_track_live_label():
    s = radio.RadioSession()
    s.live_label = "B"
    assert s.live is s.decks["B"] and s.standby is s.decks["A"]
    assert radio._other("B") == "A" and radio._other("A") == "B"


def test_reset_clears_dual_state():
    s = radio.RadioSession()
    s.dual_deck = True; s.live_label = "B"; s.epoch = 4; s.standby_dirty = True
    s.decks["A"]["queue"].append({"key": "k"})
    s.reset()
    assert s.dual_deck is False and s.live_label == "A" and s.epoch == 0 and s.standby_dirty is False
    assert s.decks["A"]["queue"] == []


def test_waiting_defaults_false_and_reset_clears_it():
    # Waiting-state net: `waiting` is reset-able session state (set True by a "deck-waiting" pevent,
    # cleared by the next live play frame), and must never survive a reset (stop/restart/disconnect).
    s = radio.RadioSession()
    assert s.waiting is False
    s.waiting = True
    s.reset()
    assert s.waiting is False


def test_toggle_swaps_and_marks_dirty(monkeypatch, store):
    rec_params.set_param(store, "radio_deck_size", 3)
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, 7)]))
    s = radio.RadioSession()
    radio.start_dual_session(store, s, now=1.0)       # A=1,2,3 live ; B=4,5,6 standby
    out = radio.toggle_decks(s)
    assert out == {"new_live": "B", "epoch": 1}
    assert s.live_label == "B" and s.pos == 0 and s.standby_dirty is True
    assert {"k1", "k2", "k3"} <= s.dispatched_keys    # old live deck A folded into no-repeat


def test_rebuild_standby_changes_and_stamps_epoch(monkeypatch, store):
    rec_params.set_param(store, "radio_deck_size", 2)
    # start: A=1,2 ; B=3,4 . toggle -> live B ; rebuild standby (A) from fresh picks 5,6.
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, 7)]))
    s = radio.RadioSession()
    radio.start_dual_session(store, s, now=1.0)
    s.decks["A"]["applied_vids"] = ["v1", "v2"]        # simulate A having been reconciled at start
    radio.toggle_decks(s)                              # live=B, standby=A (dirty)
    plan = radio.rebuild_standby(store, s, now=2.0)
    assert plan["playlist_key"] == "A"
    assert plan["vids"] == ["v5", "v6"]                # A rebuilt disjoint from live B(3,4) + played
    assert plan["boundary"] == "v6"
    assert plan["epoch"] == 1                          # stamped with the post-toggle epoch
    assert s.standby_dirty is False


def test_rebuild_standby_none_when_unchanged(monkeypatch, store):
    rec_params.set_param(store, "radio_deck_size", 2)
    monkeypatch.setattr(radio, "pick_next", _picker([_pk(str(i)) for i in range(1, 5)]))
    s = radio.RadioSession()
    radio.start_dual_session(store, s, now=1.0)        # A=1,2 live ; B=3,4 standby
    s.decks["B"]["applied_vids"] = ["v3", "v4"]
    # No fresh picks available (picker exhausted) -> rebuild rolls back to the prior queue -> None.
    plan = radio.rebuild_standby(store, s, now=2.0)
    assert plan is None
    assert [p["video_id"] for p in s.decks["B"]["queue"]] == ["v3", "v4"]


def test_rebuild_standby_purges_skip_tainted_pick(monkeypatch, store):
    """rebuild_standby fully clears and re-seeds the standby (unlike _rebuild_tail_at's conditional tail
    drop, this is a whole-deck delete-rebuild), so a pick already sitting in standby that a skip
    recorded since it was queued would no longer choose is purged: it is simply not re-picked."""
    rec_params.set_param(store, "radio_deck_size", 2)

    def tainted_picker(st, se, now):
        # Model shift: once a skip against "a3" is recorded, v3 is no longer offered; v5 replaces it.
        tainted = any(a == "a3" for a, _m, _ts in se.skips)
        order = [_pk("1"), _pk("2"), _pk("4"), _pk("5"), _pk("3"), _pk("6")] if tainted else \
            [_pk("1"), _pk("2"), _pk("3"), _pk("4"), _pk("5"), _pk("6")]
        excl = radio._exclusions(se)
        for p in order:
            if p["key"] not in excl:
                return p
        return None

    monkeypatch.setattr(radio, "pick_next", tainted_picker)
    s = radio.RadioSession()
    radio.start_dual_session(store, s, now=1.0)         # A=[v1,v2] live ; B=[v3,v4] standby
    s.skips.append(("a3", None, 1.0))                   # a skip against v3's artist lands after seeding
    plan = radio.rebuild_standby(store, s, now=2.0)
    assert plan is not None
    assert "v3" not in plan["vids"]                     # the tainted pick never reappears
    assert plan["vids"] == ["v4", "v5"]
    assert s.standby_dirty is False
