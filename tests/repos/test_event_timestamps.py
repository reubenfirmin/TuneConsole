"""#85 timestamped transient-event sources: real play_events timestamps with day-model fallback."""
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:"); s.init_schema()
    s.upsert_identity("main", "bridge", None, True)
    return s


def test_plays_with_ts_prefers_real_timestamps():
    s = _store()
    # day-model row at noon of day 100; live play_event later the same day with a REAL timestamp
    s.record_history_plays(1, 100 * 86400 + 50000, ["song|artist"])
    s.record_play_event(1, "song|artist", "v1", 100 * 86400 + 61000)
    out = s.recent_plays_with_ts()
    assert out == [("song|artist", 100 * 86400 + 61000)]


def test_plays_with_ts_day_model_fallback_and_order():
    s = _store()
    s.record_history_plays(1, 100 * 86400 + 50000, ["old|a"])      # noon bucket only
    s.record_play_event(1, "new|b", "v2", 101 * 86400 + 3600.0)
    out = s.recent_plays_with_ts()
    assert [k for k, _ in out] == ["new|b", "old|a"]
    assert out[1][1] == 100 * 86400 + 43200                        # noon-of-day fallback ts


def test_plays_with_ts_limit():
    s = _store()
    for i in range(5):
        s.record_play_event(1, f"k{i}|a", None, 1000.0 + i * 4000)
    assert len(s.recent_plays_with_ts(limit=2)) == 2


# --- #93v2 exclude_list_ids on recent_plays_with_ts: machine-queued radio plays are not taste
# evidence, and their day-granular history_items shadows (recorded provenance-free by the next YTM
# sync) must not resurrect them through the union's MAX(ts) GROUP BY key dedupe. ---

def test_exclusion_drops_radio_play_and_keeps_null_and_other_provenance():
    s = _store()
    s.record_play_event(1, "radio|x", "v1", 100 * 86400 + 1000, playlist_ytm_id="PLRADIO")
    s.record_play_event(1, "other|y", "v2", 100 * 86400 + 2000, playlist_ytm_id="PLOTHER")
    s.record_play_event(1, "none|z", "v3", 100 * 86400 + 3000, playlist_ytm_id=None)
    out = s.recent_plays_with_ts(exclude_list_ids={"PLRADIO"})
    assert [k for k, _ in out] == ["none|z", "other|y"]


def test_radio_play_stays_excluded_when_history_shadow_lands_same_day():
    # The re-entry case: radio plays a track (provenance PLRADIO), then the next YTM history sync
    # re-records that same play as a provenance-free history_items noon-bucket row for the same key
    # and day. The union must NOT resurrect the play through that shadow.
    s = _store()
    day = 100 * 86400
    s.record_play_event(1, "radio|x", "v1", day + 61000, playlist_ytm_id="PLRADIO")
    s.record_history_plays(1, day + 70000, ["radio|x"])        # the post-sync shadow, same UTC day
    assert s.recent_plays_with_ts(exclude_list_ids={"PLRADIO"}) == []


def test_radio_play_stays_excluded_when_shadow_lands_one_day_off():
    # M1 timezone edge: YTM buckets plays by the account's LOCAL day, but _parse_played_date anchors
    # 'Today'/'Yesterday' on the UTC sync day, so a non-UTC evening radio play's history shadow can
    # land one UTC day off in EITHER direction. The adjacent-day (+/- 1) widening must suppress both.
    for shadow_day_offset in (-1, +1):
        s = _store()
        day = 100 * 86400
        s.record_play_event(1, "radio|x", "v1", day + 61000, playlist_ytm_id="PLRADIO")
        # the shadow bucketed to the adjacent UTC day (record_history_plays keys on the sync day)
        s.record_history_plays(1, day + shadow_day_offset * 86400 + 70000, ["radio|x"])
        assert s.recent_plays_with_ts(exclude_list_ids={"PLRADIO"}) == [], \
            f"shadow at day offset {shadow_day_offset:+d} must stay suppressed"


def test_play_events_identity_key_index_exists():
    # H1: the shadow-suppression NOT EXISTS probes play_events by identity_key once per history row;
    # without this index that is a correlated full scan (measured ~25 s at 33k history rows x 20k
    # events, vs 67 ms indexed), run four times per ranking pass once radio settings exist.
    s = _store()
    indexed_cols = {r["name"]: [c["name"] for c in s.conn.execute(f"PRAGMA index_info({r['name']})")]
                    for r in s.conn.execute("PRAGMA index_list(play_events)")}
    assert indexed_cols.get("ix_play_events_key") == ["identity_key"]


def test_history_row_without_matching_radio_event_survives_exclusion():
    # Pre-live/organic history (no radio play_event on that key/day) must pass through untouched.
    s = _store()
    s.record_history_plays(1, 100 * 86400 + 50000, ["organic|a"])
    out = s.recent_plays_with_ts(exclude_list_ids={"PLRADIO"})
    assert out == [("organic|a", 100 * 86400 + 43200)]


def test_shadow_suppression_is_day_scoped_not_key_global():
    # Same key: an organic history play on day 100, then a radio-queued play on day 102. Only day
    # 102's rows disappear; the day-100 organic listen still counts (with its own noon timestamp).
    s = _store()
    s.record_history_plays(1, 100 * 86400 + 50000, ["song|a"])   # organic, day 100
    s.record_play_event(1, "song|a", "v1", 102 * 86400 + 61000, playlist_ytm_id="PLRADIO")
    s.record_history_plays(1, 102 * 86400 + 70000, ["song|a"])   # day-102 shadow of the radio play
    out = s.recent_plays_with_ts(exclude_list_ids={"PLRADIO"})
    assert out == [("song|a", 100 * 86400 + 43200)]              # day-100 noon ts, not day 102's


def test_empty_exclusion_is_identical_to_no_exclusion_arg():
    s = _store()
    s.record_history_plays(1, 100 * 86400 + 50000, ["song|a"])
    s.record_play_event(1, "radio|x", "v1", 100 * 86400 + 61000, playlist_ytm_id="PLRADIO")
    baseline = s.recent_plays_with_ts()
    assert s.recent_plays_with_ts(exclude_list_ids=None) == baseline
    assert s.recent_plays_with_ts(exclude_list_ids=[]) == baseline
    assert s.recent_plays_with_ts(exclude_list_ids=set()) == baseline
    assert s.recent_plays_with_ts(limit=1, exclude_list_ids=None) == baseline[:1]   # limit still binds


def test_exclusion_respects_limit_param():
    s = _store()
    for i in range(5):
        s.record_play_event(1, f"k{i}|a", None, 1000.0 + i * 4000)
    s.record_play_event(1, "radio|x", "v1", 999999.0, playlist_ytm_id="PLRADIO")
    out = s.recent_plays_with_ts(limit=2, exclude_list_ids={"PLRADIO"})
    assert len(out) == 2 and all(k != "radio|x" for k, _ in out)


def test_liked_and_disliked_with_ts():
    s = _store()
    s.record_like("liked|a", 5000.0, provenance="action")   # live-observed: has a real event time
    s.record_like("bulk|c", 5500.0)                         # sync-discovered: no real event time
    s.record_dislike("bad|b", 99999.0, 6000.0)
    # only the action-provenance like reaches the transient reader (sync likes have no true
    # timestamp, so they must not masquerade as fresh user actions)
    assert s.recent_liked_with_ts() == [("liked|a", 5000.0)]
    assert s.disliked_with_ts() == [("bad|b", 6000.0)]
