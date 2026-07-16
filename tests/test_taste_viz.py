"""#88 The taste-model transparency payload: the per-facet multiplier chain (PERMANENT x TRANSIENT),
the modes axis at the three timescales it exists on (NOW / SESSION / PERMANENT), and the embedding
axis at the two it exists on (SESSION / TRANSIENT).
"""
import numpy as np
import pytest

from yt_playlist.rec import embed, layers, rec_params, scoring, taste_viz, transient


def _install_now_modes(store):
    """Two orthogonal active taste modes in a 2-D content space (mirrors test_now_layer.py)."""
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Warehouse techno", "families": [["techno", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 60, "rep_keys": []},
        {"mode_id": 2, "label": "Chill acoustic", "families": [["folk", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 20, "rep_keys": []},
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


# ── The facet axes: PERMANENT x TRANSIENT = EFFECTIVE ───────────────────────

def test_chain_multiplies_permanent_by_standing_by_transient(store):
    _seed_jazz(store)
    store.set_weight("genre:jazz", 1.5)
    store.set_lean("genre:jazz", 1.2, 1000.0)
    payload = taste_viz.model_transparency(store, now=1000.0)
    jazz = next(r for r in payload["genres"] if r["name"] == "jazz")
    assert jazz["permanent_weight"] == pytest.approx(1.5)
    assert jazz["standing_lean"] == pytest.approx(1.2)
    assert jazz["lasting"] == pytest.approx(1.5 * 1.2)
    assert jazz["effective"] == pytest.approx(1.5 * 1.2 * jazz["transient_mult"])


def test_effective_equals_the_multiplier_scoring_actually_applies(store):
    """The whole point of the card: `effective` must be the number `scoring._axis_mult` composes, not
    a re-clamped lookalike. A weight above the genre band would previously be shown clamped here and
    applied unclamped there."""
    _seed_jazz(store)
    store.set_weight("genre:jazz", 1.9)
    store.set_lean("genre:jazz", 1.4, 1000.0)
    now = 1000.0

    row = next(r for r in taste_viz.model_transparency(store, now=now)["genres"] if r["name"] == "jazz")

    weights = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    gw = {a[len("genre:"):]: v for a, v in weights.items() if a.startswith("genre:")}
    fparams = (rec_params.get_param(store, "facet_gain"),
               rec_params.get_param(store, "facet_mult_min"),
               rec_params.get_param(store, "facet_mult_max"))
    expected = scoring._axis_mult(gw, "genre", "jazz", store.get_leans(),
                                  transient.facet_leans(store, now), fparams)
    assert row["effective"] == pytest.approx(expected)


def test_transient_multiplier_reflects_a_recent_play(store):
    """A recent play pushes its genre's transient lean positive, so the TRANSIENT rose has a shape
    while PERMANENT stays neutral. This is the contrast the card exists to draw."""
    _seed_jazz(store)
    row = next(r for r in taste_viz.model_transparency(store, now=1.0)["genres"] if r["name"] == "jazz")
    assert row["transient_lean"] > 0
    assert row["transient_mult"] > 1.0
    assert row["lasting"] == pytest.approx(1.0)      # nothing graduated, no slider held
    assert row["effective"] == pytest.approx(row["transient_mult"])


def test_cold_start_is_neutral_everywhere(store):
    payload = taste_viz.model_transparency(store, now=1000.0)
    assert payload["genres"] == [] and payload["eras"] == [] and payload["artists"] == []
    assert payload["sources"]["plays"] == 0
    assert payload["modes"]["modes"] == []


def test_funnel_reports_threshold(store):
    store.bump_theme("genre:jazz", 0.6, 1000.0)
    payload = taste_viz.model_transparency(store, now=1000.0)
    row = next(r for r in payload["funnel"] if r["facet"] == "genre:jazz")
    assert row["threshold"] == rec_params.THEME_THRESHOLD
    assert abs(row["frac"] - 0.6 / rec_params.THEME_THRESHOLD) < 1e-6


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
    # Shares must be normalized over ALL artists (like genres), not just the displayed top-N, so the
    # Artists card's "what you play" bars are on the same footing as the genre rose's.
    iid = store.upsert_identity("main", "cred", None, True)
    for i in range(13):
        store.upsert_track(f"v{i}", f"S{i}", f"Art{i}", None, None)
        store.add_history_snapshot(iid, 10.0 + i, [f"s{i}|art{i}"])
    shares = dict(taste_viz._artist_shares(store, top=12))
    assert len(shares) == 12                                  # displays the top 12
    # each is 1/13 (normalized over all 13), so the 12 shown sum to 12/13, NOT 1.0
    assert abs(sum(shares.values()) - 12 / 13) < 1e-6


# ── The modes axis: NOW / SESSION / PERMANENT ───────────────────────────────

def test_mode_layers_three_ribbons_share_colors_for_the_same_mode(store, monkeypatch):
    # NOW, SESSION and PERMANENT all read shares over the SAME active-mode list, so a given mode_id
    # must carry the SAME color_idx in all three ribbons - that is what makes them stack visually.
    _install_now_modes(store)
    keys = ["a1", "a2", "b1"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [0.02, 1.0]], dtype=np.float32)
    _install_now_content_vectors(store, monkeypatch, keys, V)
    iid = store.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    # All three plays at age 0 -> decay_weight(0, ...) == 1.0 exactly, so SESSION's decay-weighted
    # shares land on the exact same 2/3, 1/3 split as NOW's plain-count posterior.
    store.import_play_events(iid, [(k, "v" + k, now) for k in keys])

    ml = taste_viz.model_transparency(store, now=now)["modes"]
    assert [m["mode_id"] for m in ml["modes"]] == [1, 2]

    by_layer = {}
    for layer in ("now", "session", "permanent"):
        by_layer[layer] = {s["mode_id"]: s for s in ml[layer]["segments"]}

    assert ml["now"]["n"] == 3 and ml["session"]["n"] == 3
    for layer in ("now", "session"):
        assert by_layer[layer][1]["share"] == pytest.approx(2 / 3, abs=1e-6)
        assert by_layer[layer][2]["share"] == pytest.approx(1 / 3, abs=1e-6)
    assert by_layer["now"][1]["label"] == "Warehouse techno"

    # PERMANENT is each mode's share of the clustered library, by size (60 and 20 -> 0.75 / 0.25).
    assert by_layer["permanent"][1]["share"] == pytest.approx(0.75)
    assert by_layer["permanent"][2]["share"] == pytest.approx(0.25)
    assert ml["permanent"]["n"] == 80

    # The color-consistency guarantee: same mode_id -> same color_idx in every ribbon.
    for mode_id, idx in ((1, 0), (2, 1)):
        assert {by_layer[l][mode_id]["color_idx"] for l in by_layer} == {idx}

    assert ml["now"]["window_h"] == rec_params.get_param(store, "now_window_h")
    assert ml["session"]["halflife_h"] == rec_params.get_param(store, "session_halflife_h")
    assert ml["min_events"] == int(rec_params.get_param(store, "now_min_events"))


def test_mode_layers_permanent_stands_alone_when_the_fast_layers_are_quiet(store):
    # Modes exist, but no recent plays -> NOW and SESSION are below the confidence gate and report
    # nothing, while PERMANENT still describes the durable shape. Quiet is not the same as absent.
    _install_now_modes(store)
    ml = taste_viz.model_transparency(store, now=1_000_000.0)["modes"]
    assert ml["now"]["segments"] == [] and ml["session"]["segments"] == []
    assert {s["mode_id"] for s in ml["permanent"]["segments"]} == {1, 2}
    assert [m["mode_id"] for m in ml["modes"]] == [1, 2]


def test_mode_layers_empty_without_modes(store):
    ml = taste_viz.model_transparency(store, now=1000.0)["modes"]
    assert ml["modes"] == []
    assert ml["now"]["segments"] == ml["session"]["segments"] == ml["permanent"]["segments"] == []


# ── The embedding axis: SESSION / TRANSIENT ─────────────────────────────────

def test_centroid_tilt_quiet_on_cold_store(store):
    panel = taste_viz.centroid_tilt_panel(store, now=1000.0)
    assert panel["families"] == []
    assert panel["has_session"] is False and panel["has_transient"] is False
    # The half-lives ride along even when quiet: the template's copy names them regardless.
    assert panel["halflife_h"] == rec_params.get_param(store, "session_halflife_h")


def _install_tilt_space(store, monkeypatch, session, transient_dir):
    """A 2-D collaborative space with two single-track genre families on the axes, so a family's
    centroid IS the axis and a projection reads off directly."""
    keys = ["t|techno", "j|jazz"]
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(taste_viz.embed, "load_vectors", lambda s: (keys, V, idx))
    monkeypatch.setattr(taste_viz, "RecDao",
                        lambda s: type("D", (), {"track_genres": staticmethod(
                            lambda ks: {"t|techno": "Techno", "j|jazz": "Jazz"})})())
    monkeypatch.setattr(taste_viz.layers, "session_tilt", lambda *a: session)
    monkeypatch.setattr(taste_viz.transient, "centroid_tilt", lambda *a: transient_dir)


def test_centroid_tilt_projects_both_layers_onto_the_same_centroids(store, monkeypatch):
    # Session leans hard toward techno; transient leans toward jazz. Both are cosines against the same
    # family centroids, so the two bars sit on one comparable scale and can visibly disagree.
    _install_tilt_space(store, monkeypatch,
                        session=np.array([1.0, 0.0]), transient_dir=np.array([0.0, 1.0]))
    panel = taste_viz.centroid_tilt_panel(store, now=1000.0)
    assert panel["has_session"] and panel["has_transient"]
    fams = {f["name"]: f for f in panel["families"]}
    assert fams["techno"]["session"] == pytest.approx(1.0, abs=1e-6)
    assert fams["techno"]["transient"] == pytest.approx(0.0, abs=1e-6)
    assert fams["jazz"]["session"] == pytest.approx(0.0, abs=1e-6)
    assert fams["jazz"]["transient"] == pytest.approx(1.0, abs=1e-6)


def test_centroid_tilt_reports_transient_alone_when_session_is_gated(store, monkeypatch):
    # A quiet session (below now_min_events) must leave the transient row intact and mark itself
    # absent, rather than suppressing the whole panel.
    _install_tilt_space(store, monkeypatch, session=None, transient_dir=np.array([0.0, 1.0]))
    panel = taste_viz.centroid_tilt_panel(store, now=1000.0)
    assert panel["has_session"] is False and panel["has_transient"] is True
    assert all(f["session"] is None for f in panel["families"])
    assert {f["name"] for f in panel["families"]} == {"techno", "jazz"}


def test_now_layer_has_no_embedding_direction(store, monkeypatch):
    """NOW is categorical over modes by design (layers.py: an hour of plays cannot place a direction
    without whiplash). Pin that the embedding panel never grows a `now` row."""
    _install_tilt_space(store, monkeypatch,
                        session=np.array([1.0, 0.0]), transient_dir=np.array([0.0, 1.0]))
    panel = taste_viz.centroid_tilt_panel(store, now=1000.0)
    assert "now" not in panel
    assert all("now" not in f for f in panel["families"])
    assert not hasattr(layers, "now_tilt")


# ── Misc ───────────────────────────────────────────────────────────────────

def test_engine_panel_reports_counts(store):
    panel = taste_viz.engine_panel(store)
    assert panel["vectors"] == store.rec_vectors_count()
    assert panel["contexts"] == []          # no playlists -> no taste contexts
    assert panel["dim"] >= 1


def test_breadth_word_thresholds():
    assert taste_viz._breadth_word(0.9) == "eclectic"
    assert taste_viz._breadth_word(0.5) == "balanced"
    assert taste_viz._breadth_word(0.1) == "focused"


def test_recent_play_counts_are_frequency_weighted(store):
    # A replayed track counts more than once (unlike the deduped recent_keys_ordered).
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Star", None, None)
    store.upsert_track("v2", "Bsong", "Other", None, None)
    for ts in (10.0, 20.0, 30.0):
        store.add_history_snapshot(iid, ts, ["hit|star"])        # played 3 times
    store.add_history_snapshot(iid, 40.0, ["bsong|other"])
    counts = store.recent_play_counts(1000)
    assert counts["hit|star"] == 3 and counts["bsong|other"] == 1
