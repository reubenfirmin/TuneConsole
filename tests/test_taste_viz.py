import numpy as np
import pytest

from yt_playlist.rec import embed, rec_params, taste_viz


def _install_now_modes(store):
    """Two orthogonal active taste modes in a 2-D content space (mirrors test_now_layer.py)."""
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Warehouse techno", "families": [["techno", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
        {"mode_id": 2, "label": "Chill acoustic", "families": [["folk", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)


def _install_now_content_vectors(store, monkeypatch, keys, V):
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s: (keys, V, idx))
    return V, idx


def _seed_jazz(store):
    """A jazz track that has been played, so it registers in the play-weighted genre distribution."""
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Jazz")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    return iid


def test_viz_reflects_param_overrides(store):
    # #85: stale_decay_halflife_d and the "freshness" payload key are gone (no sync-staleness relax
    # any more); the equivalent per-source override now shows up in sources.halflife_days.
    store.upsert_identity("main", "cred", None, True)
    rec_params.set_param(store, "mood_halflife_d", 10)
    payload = taste_viz.model_transparency(store, now=1000.0)
    assert payload["sources"]["halflife_days"]["mood"] == 10


def test_layer_stack_multiplies(store):
    _seed_jazz(store)
    store.set_weight("genre:jazz", 1.5)
    store.set_lean("genre:jazz", 1.2, 1000.0)
    payload = taste_viz.model_transparency(store, now=1000.0)
    jazz = next(r for r in payload["genres"] if r["name"] == "jazz")
    assert abs(jazz["permanent_weight"] - 1.5) < 1e-9
    assert abs(jazz["standing_lean"] - 1.2) < 1e-9
    expected = (min(rec_params.GENRE_MAX, 1.5) * min(rec_params.GENRE_MAX, 1.2)
                * jazz["transient_mult"])
    assert abs(jazz["effective"] - expected) < 1e-6


def test_cold_start_has_no_transient(store):
    payload = taste_viz.model_transparency(store, now=1000.0)
    assert payload["has_transient"] is False
    assert payload["recent_exists"] is False
    assert payload["sources"]["plays"] == 0
    assert payload["genres"] == []
    # #85: no "freshness" key any more (no sync-staleness relax); nothing to assert here beyond the
    # above - a cold store's sources are simply empty.


def test_transient_deviation_is_zero_sum_and_signed(store):
    # All-time: jazz dominates (3 plays vs 1). Recent window of 2 events = the latest techno + jazz
    # play -> recent mix is 50/50, so techno is OVER-indexed and jazz UNDER-indexed, equal & opposite.
    iid = store.upsert_identity("main", "cred", None, True)
    j = store.upsert_track("vj", "JTrack", "JArtist", None, None)
    store.set_track_genre(j, "Jazz")
    t = store.upsert_track("vt", "TTrack", "TArtist", None, None)
    store.set_track_genre(t, "Techno")
    for ts in (10.0, 20.0, 30.0):
        store.add_history_snapshot(iid, ts, ["jtrack|jartist"])
    store.add_history_snapshot(iid, 40.0, ["ttrack|tartist"])    # most recent play
    p = taste_viz.model_transparency(store, now=100.0, recent_window=2)
    g = {r["name"]: r for r in p["genres"]}
    assert g["techno"]["transient_dev"] > 0          # techno: 50% recent vs 25% all-time
    assert g["jazz"]["transient_dev"] < 0            # jazz: 50% recent vs 75% all-time
    assert abs(g["techno"]["transient_dev"] + g["jazz"]["transient_dev"]) < 1e-9   # zero-sum
    assert p["has_transient"] is True and p["recent_exists"] is True


def test_recent_play_counts_are_frequency_weighted(store):
    # A replayed track counts more than once (unlike the deduped recent_keys_ordered) -- the basis the
    # recent-vs-usual deviation needs so heavy rotation isn't flattened to mere presence.
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Star", None, None)
    store.upsert_track("v2", "Bsong", "Other", None, None)
    for ts in (10.0, 20.0, 30.0):
        store.add_history_snapshot(iid, ts, ["hit|star"])        # played 3 times
    store.add_history_snapshot(iid, 40.0, ["bsong|other"])
    counts = store.recent_play_counts(1000)
    assert counts["hit|star"] == 3 and counts["bsong|other"] == 1


def test_funnel_reports_threshold(store):
    store.bump_theme("genre:jazz", 0.6, 1000.0)
    payload = taste_viz.model_transparency(store, now=1000.0)
    row = next(r for r in payload["funnel"] if r["facet"] == "genre:jazz")
    assert row["threshold"] == rec_params.THEME_THRESHOLD
    assert abs(row["frac"] - 0.6 / rec_params.THEME_THRESHOLD) < 1e-6


def test_engine_panel_reports_counts(store):
    panel = taste_viz.engine_panel(store)
    assert panel["vectors"] == store.rec_vectors_count()
    assert panel["contexts"] == []          # no playlists -> no taste contexts
    assert panel["dim"] >= 1


def test_centroid_tilt_quiet_on_cold_store(store):
    panel = taste_viz.centroid_tilt_panel(store, now=1000.0)
    assert panel == {"magnitude": 0.0, "projection": []}


def test_single_genre_has_no_shift(store):
    # With one genre, recent and all-time mixes are both 100% it -> zero deviation -> quiet (no
    # dramatic petal). Proves the deviation view can't manufacture a shift from a one-genre library.
    _seed_jazz(store)
    assert taste_viz.model_transparency(store, now=100.0)["recent_exists"] is True
    assert taste_viz.model_transparency(store, now=100.0)["has_transient"] is False


def test_artists_populate_from_play_history(store):
    # Regression: _artist_shares read the wrong dict key, so the Artists panel was always empty.
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("va", "ASong", "Alice", None, None)
    store.upsert_track("vb", "BSong", "Bob", None, None)
    for ts in (10.0, 20.0, 30.0):
        store.add_history_snapshot(iid, ts, ["asong|alice"])      # Alice played more
    store.add_history_snapshot(iid, 40.0, ["bsong|bob"])
    arts = {r["name"]: r for r in taste_viz.model_transparency(store, now=100.0)["artists"]}
    assert "Alice" in arts and "Bob" in arts
    assert arts["Alice"]["share"] > arts["Bob"]["share"]
    assert abs(arts["Alice"]["share"] + arts["Bob"]["share"] - 1.0) < 1e-9


def test_artist_shares_normalize_over_all_artists(store):
    # All-time artist shares must be normalized over ALL artists (like genres), not just the displayed
    # top-N. Otherwise recent_share (full population) is systematically below all-time_share (top-N
    # base) and every artist reads as "less than usual" -- the bug. 13 artists, one play each:
    iid = store.upsert_identity("main", "cred", None, True)
    for i in range(13):
        store.upsert_track(f"v{i}", f"S{i}", f"Art{i}", None, None)
        store.add_history_snapshot(iid, 10.0 + i, [f"s{i}|art{i}"])
    shares = dict(taste_viz._artist_shares(store, top=12))
    assert len(shares) == 12                                  # displays the top 12
    # each is 1/13 (normalized over all 13), so the 12 shown sum to 12/13, NOT 1.0
    assert abs(sum(shares.values()) - 12 / 13) < 1e-6


def test_now_layer_present_with_live_posterior(store, monkeypatch):
    # #88 Task 5: a live NOW posterior surfaces as a terse `now_layer` reading on the transparency
    # payload - top mode label, its share, and how many distinct plays fed it.
    _install_now_modes(store)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    _install_now_content_vectors(store, monkeypatch, keys, V)
    iid = store.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.import_play_events(iid, rows)

    payload = taste_viz.model_transparency(store, now=now)
    assert payload["now_layer"] == {"top_label": "Warehouse techno", "top_share": pytest.approx(1.0), "n": 3}
    assert payload["now_window_h"] == rec_params.get_param(store, "now_window_h")


def test_now_layer_absent_when_quiet(store):
    # No modes, no plays -> below the confidence gate -> None, not a weak guess.
    payload = taste_viz.model_transparency(store, now=1000.0)
    assert payload["now_layer"] is None


def test_layer_stack_now_and_session_share_colors_for_same_mode(store, monkeypatch):
    # #88: NOW and SESSION both classify the SAME three plays to the SAME two modes, so their ribbon
    # segments must carry the SAME color_idx per mode_id - that's the whole point of layer_modes.
    _install_now_modes(store)
    keys = ["a1", "a2", "b1"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [0.02, 1.0]], dtype=np.float32)
    _install_now_content_vectors(store, monkeypatch, keys, V)
    iid = store.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    # All three plays at age 0 -> decay_weight(0, ...) == 1.0 exactly, so SESSION's decay-weighted
    # shares land on the exact same 2/3, 1/3 split as NOW's plain-count posterior.
    store.import_play_events(iid, [(k, "v" + k, now) for k in keys])

    payload = taste_viz.model_transparency(store, now=now)
    ls = payload["layer_stack"]

    assert [m["mode_id"] for m in ls["layer_modes"]] == [1, 2]

    assert ls["now"]["n"] == 3
    now_by_mode = {s["mode_id"]: s for s in ls["now"]["segments"]}
    assert now_by_mode[1]["share"] == pytest.approx(2 / 3, abs=1e-6)
    assert now_by_mode[2]["share"] == pytest.approx(1 / 3, abs=1e-6)
    assert now_by_mode[1]["label"] == "Warehouse techno"
    assert now_by_mode[2]["label"] == "Chill acoustic"

    assert ls["session"]["n"] == 3
    session_by_mode = {s["mode_id"]: s for s in ls["session"]["segments"]}
    assert session_by_mode[1]["share"] == pytest.approx(2 / 3, abs=1e-6)
    assert session_by_mode[2]["share"] == pytest.approx(1 / 3, abs=1e-6)

    # The color-consistency guarantee: same mode_id -> same color_idx in both ribbons.
    assert now_by_mode[1]["color_idx"] == session_by_mode[1]["color_idx"] == 0
    assert now_by_mode[2]["color_idx"] == session_by_mode[2]["color_idx"] == 1

    assert ls["now"]["window_h"] == rec_params.get_param(store, "now_window_h")
    assert ls["session"]["halflife_h"] == rec_params.get_param(store, "session_halflife_h")
    assert ls["min_events"] == int(rec_params.get_param(store, "now_min_events"))


def test_layer_stack_quiet_now_and_session_when_no_modes_or_plays(store):
    payload = taste_viz.model_transparency(store, now=1000.0)
    ls = payload["layer_stack"]
    assert ls["now"]["segments"] == []
    assert ls["session"]["segments"] == []
    assert ls["layer_modes"] == []


def test_layer_stack_transient_and_permanent_summarize_existing_data(store):
    # #88: TRANSIENT/PERMANENT rows are summaries of data model_transparency already computes (the
    # genre roses / breadth), not new computations - reuse the same fixture as
    # test_transient_deviation_is_zero_sum_and_signed (techno over-indexed, jazz under-indexed).
    iid = store.upsert_identity("main", "cred", None, True)
    j = store.upsert_track("vj", "JTrack", "JArtist", None, None)
    store.set_track_genre(j, "Jazz")
    t = store.upsert_track("vt", "TTrack", "TArtist", None, None)
    store.set_track_genre(t, "Techno")
    for ts in (10.0, 20.0, 30.0):
        store.add_history_snapshot(iid, ts, ["jtrack|jartist"])
    store.add_history_snapshot(iid, 40.0, ["ttrack|tartist"])
    payload = taste_viz.model_transparency(store, now=100.0, recent_window=2)
    ls = payload["layer_stack"]

    assert ls["transient"]["up"]["name"] == "techno"
    assert ls["transient"]["down"]["name"] == "jazz"

    assert ls["permanent"]["top"]["name"] == "jazz"     # jazz has the larger all-time share (3 vs 1)
    assert ls["permanent"]["breadth_word"] == taste_viz._breadth_word(payload["breadth"])


def test_layer_stack_transient_none_when_quiet(store):
    # No plays at all -> genres is empty -> no up/down facet, no permanent top.
    payload = taste_viz.model_transparency(store, now=1000.0)
    ls = payload["layer_stack"]
    assert ls["transient"]["up"] is None
    assert ls["transient"]["down"] is None
    assert ls["permanent"]["top"] is None
    assert ls["permanent"]["breadth_word"] is None


def test_breadth_word_thresholds():
    assert taste_viz._breadth_word(0.9) == "eclectic"
    assert taste_viz._breadth_word(0.5) == "balanced"
    assert taste_viz._breadth_word(0.1) == "focused"
