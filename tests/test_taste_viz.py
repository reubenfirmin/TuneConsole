"""#88 The taste-model transparency payload: the per-facet multiplier chain (PERMANENT x TRANSIENT),
the modes axis at the three timescales it exists on (NOW / SESSION / PERMANENT), and the embedding
axis at the two it exists on (SESSION / TRANSIENT).
"""
import numpy as np
import pytest

from yt_playlist.rec import embed, graduation, layers, rec_params, scoring, taste_viz, transient


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
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    return V, idx


def _seed_jazz(store):
    """A jazz track that has been played, so it registers in the play-weighted genre distribution."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Jazz")
    store.history.add_history_snapshot(iid, 1.0, ["song|band"])
    return iid


def test_learning_progress_groups_all_evidence_without_reinterpreting_genres(store):
    for i in range(15):
        store.rec_surface.bump_theme(f"artist:Artist {i:02}", .1 + i * .01, 1.0)
    store.rec_surface.bump_theme("artist:AC:DC", -.9, 1.0)
    store.rec_surface.bump_theme("genre:other:alternative metal", .6, 1.0)
    store.rec_surface.bump_theme("era:1990", 1.8, 1.0)
    data = taste_viz.learning_progress(store, 1.0)
    artists, genres, eras = data["groups"]
    assert len(artists["rows"]) == 16  # pagination must not discard the rest
    assert artists["rows"][0] == {"facet": "artist:AC:DC", "name": "AC:DC",
                                  "score": -.9, "percent": 75, "direction": "reduction",
                                  "updated_at": 1.0}
    assert genres["rows"][0]["name"] == "alternative metal"
    assert genres["rows"][0]["facet"] == "genre:other:alternative metal"
    assert genres["rows"][0]["percent"] == 50
    assert eras["rows"][0]["name"] == "1990s"
    assert eras["rows"][0]["percent"] == 100
    # Accumulated learning evidence stays put as recent influence fades.
    assert taste_viz.learning_progress(store, 100_000_000)["groups"] == data["groups"]


@pytest.mark.parametrize("axis", ["artist", "genre", "era"])
def test_learning_progress_orders_by_latest_evidence_not_strength(store, axis):
    for name, score, updated_at in (("Strong boost", .9, 10), ("Strong reduction", -.9, 20),
                                    ("Small boost", .1, 30), ("Small reduction", -.1, 40),
                                    ("Epoch", .3, 0)):
        store.rec_surface.bump_theme(f"{axis}:{name}", score, updated_at)

    def rows():
        return next(group["rows"] for group in taste_viz.learning_progress(store, 100)["groups"]
                    if group["axis"] == axis)

    assert [row["name"] for row in rows()] == ["Small reduction", "Small boost", "Strong reduction",
                                               "Strong boost", "Epoch"]
    assert [row["updated_at"] for row in rows()] == [40, 30, 20, 10, 0]
    store.rec_surface.bump_theme(f"{axis}:Strong boost", .001, 50)
    assert rows()[0]["name"] == "Strong boost"
    assert rows()[0]["updated_at"] == 50


def test_learning_source_curves_follow_configured_half_lives(store):
    rec_params.set_param(store, "play_halflife_d", 7)
    data = taste_viz.learning_progress(store, 1.0)
    assert [row["title"] for row in data["activity"]] == ["Plays", "Likes", "Mood choices", "Dislikes", "Skips"]
    play = data["activity"][0]
    assert play["half_days"] == 7
    assert "50.00 34.00" in play["curve"]  # day 7 of 28: half of the 60px influence remains
    assert play["curve"].endswith("200.00 60.25")  # four half-lives: 1/16 remains
    assert all(row["count"] == 0 for row in data["activity"])
    assert all(not group["rows"] for group in data["groups"])


def test_other_learning_shows_choices_faded_sources_and_completed_steps(store):
    _install_now_modes(store)
    store.modes.log_impressions(1, [("a", 1), ("b", 2)], 10)
    store.modes.log_pick(1, 2, 20)
    store.rec_model.set_weight("lane:deep_cut", 2, now=100)
    graduation.graduate_facet(store, "genre:other:alternative metal", 2, 40, source_label="like")
    graduation.graduate_facet(store, "era:1990", -2, 30, source_label="dislike")
    graduation.graduate_facet(store, "artist:Unfinished", .01, 50)
    now = 100 + rec_params.get_param(store, "weight_revert_halflife_d") * 86400
    data = taste_viz.learning_progress(store, now)
    assert data["choices"]["picked"] == 1 and data["choices"]["offered"] == 2
    assert [row["mode_id"] for row in data["choices"]["rows"]] == [2, 1]
    source = data["source_weights"]["lanes"][0]
    assert source["name"] == "deep_cut" and source["updated_at"] == 100
    assert source["saved_weight"] == 2 and source["weight"] == pytest.approx(1.5)
    assert [row["updated_at"] for row in data["changes"]] == [40, 30]
    assert [(row["name"], row["direction"], row["source"]) for row in data["changes"]] == [
        ("alternative metal", "boost", "Likes"), ("1990s", "reduction", "Dislikes")]
    assert data["changes"][0]["new_weight"] == rec_params.get_param(store, "graduate_up")


def test_overview_reports_recent_shifts_against_library_not_a_second_library_portrait(store, monkeypatch):
    _install_now_modes(store)
    keys = ["techno|artist", "folk1|artist", "folk2|artist"]
    _install_now_content_vectors(store, monkeypatch, keys,
                                 np.array([[1., 0.], [0., 1.], [0., 1.]], dtype=np.float32))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.history.import_play_events(iid, [(key, f"v{i}", 1000.0) for i, key in enumerate(keys)])
    recent = taste_viz.overview(store, 1000.0)["recent"]
    by_label = {row["label"]: row for row in recent["shifts"]}
    acoustic, techno = by_label["Chill acoustic"], by_label["Warehouse techno"]
    assert acoustic["library"] == pytest.approx(.25)
    assert acoustic["recent"] == pytest.approx(2 / 3)
    assert acoustic["delta"] == pytest.approx(2 / 3 - .25)
    assert techno["library"] == pytest.approx(.75)
    assert techno["recent"] == pytest.approx(1 / 3)
    assert recent["n"] == 3


def test_viz_reflects_param_overrides(store):
    # #85: stale_decay_halflife_d and the "freshness" payload key are gone (no sync-staleness relax
    # any more); the equivalent per-source override now shows up in sources.halflife_days.
    store.identities.upsert_identity("main", "cred", None, True)
    rec_params.set_param(store, "mood_halflife_d", 10)
    payload = taste_viz.model_transparency(store, now=1000.0)
    assert payload["sources"]["halflife_days"]["mood"] == 10


# ── The facet axes: PERMANENT x TRANSIENT = EFFECTIVE ───────────────────────

def test_chain_multiplies_permanent_by_standing_by_transient(store):
    _seed_jazz(store)
    store.rec_model.set_weight("genre:jazz", 1.5)
    store.rec_model.set_lean("genre:jazz", 1.2, 1000.0)
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
    store.rec_model.set_weight("genre:jazz", 1.9)
    store.rec_model.set_lean("genre:jazz", 1.4, 1000.0)
    now = 1000.0

    row = next(r for r in taste_viz.model_transparency(store, now=now)["genres"] if r["name"] == "jazz")

    weights = store.rec_model.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    gw = {a[len("genre:"):]: v for a, v in weights.items() if a.startswith("genre:")}
    fparams = (rec_params.get_param(store, "facet_gain"),
               rec_params.get_param(store, "facet_mult_min"),
               rec_params.get_param(store, "facet_mult_max"))
    expected = scoring._axis_mult(gw, "genre", "jazz", store.rec_model.get_leans(),
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


@pytest.mark.parametrize("saved", [1.8, 0.4])
def test_lane_provenance_matches_the_served_weight_after_fading(store, saved):
    rec_params.set_param(store, "weight_revert_halflife_d", 30)
    store.rec_model.set_weight("lane:deep_cut", saved, now=1000.0)
    now = 1000.0 + 30 * 86400
    payload = taste_viz.model_transparency(store, now=now)
    lane = next(r for r in payload["lanes"] if r["name"] == "deep_cut")
    served = store.rec_model.get_weights(now=now, revert_halflife_d=30)["lane:deep_cut"]
    assert payload["lane_weight_halflife_days"] == 30
    assert lane["has_record"] is True
    assert lane["saved_weight"] == saved
    assert lane["age_days"] == 30
    assert lane["weight"] == pytest.approx(1 + (saved - 1) / 2)
    assert lane["weight"] == served
    assert lane["fade_delta"] == pytest.approx(served - saved)


def test_lane_provenance_distinguishes_default_from_saved_neutral(store):
    store.rec_model.set_weight("lane:deep_cut", 1.0, now=1.0)
    lanes = {r["name"]: r for r in taste_viz.model_transparency(store, now=1.0)["lanes"]}
    assert lanes["deep_cut"]["has_record"] is True
    assert lanes["rotation"]["has_record"] is False
    assert lanes["rotation"]["saved_weight"] == lanes["rotation"]["weight"] == 1.0
    assert lanes["rotation"]["age_days"] is None


def test_funnel_reports_threshold(store):
    store.rec_surface.bump_theme("genre:jazz", 0.6, 1000.0)
    payload = taste_viz.model_transparency(store, now=1000.0)
    row = next(r for r in payload["funnel"] if r["facet"] == "genre:jazz")
    assert row["threshold"] == rec_params.THEME_THRESHOLD
    assert abs(row["frac"] - 0.6 / rec_params.THEME_THRESHOLD) < 1e-6


def test_artists_populate_from_play_history(store):
    # Regression: _artist_shares read the wrong dict key, so the Artists panel was always empty.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("va", "ASong", "Alice", None, None)
    store.tracks.upsert_track("vb", "BSong", "Bob", None, None)
    day = 86400.0
    for ts in (day, 2 * day, 3 * day):
        store.history.add_history_snapshot(iid, ts, ["asong|alice"])
    store.history.add_history_snapshot(iid, 4 * day, ["bsong|bob"])
    arts = {r["name"]: r for r in taste_viz.model_transparency(store, now=5 * day)["artists"]}
    assert "Alice" in arts and "Bob" in arts
    assert arts["Alice"]["share"] > arts["Bob"]["share"]
    assert abs(arts["Alice"]["share"] + arts["Bob"]["share"] - 1.0) < 1e-9


def test_artist_shares_normalize_over_all_artists(store):
    # Shares must be normalized over ALL artists (like genres), not just the displayed top-N, so the
    # Artists card's "what you play" bars are on the same footing as the genre rose's.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    for i in range(13):
        store.tracks.upsert_track(f"v{i}", f"S{i}", f"Art{i}", None, None)
        store.history.add_history_snapshot(iid, 10.0 + i, [f"s{i}|art{i}"])
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
    iid = store.identities.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    # All three plays at age 0 -> decay_weight(0, ...) == 1.0 exactly, so SESSION's decay-weighted
    # shares land on the exact same 2/3, 1/3 split as NOW's plain-count posterior.
    store.history.import_play_events(iid, [(k, "v" + k, now) for k in keys])

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
    assert ml["evidence"]["now"]["total"] == 3
    assert ml["evidence"]["now"]["mapped"] == 3
    assert ml["evidence"]["now"]["coverage"] == pytest.approx(1.0)


def test_mode_evidence_reports_unmapped_realtime_plays(store, monkeypatch):
    _install_now_modes(store)
    _install_now_content_vectors(store, monkeypatch, ["mapped"], np.array([[1.0, 0.0]]))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.history.import_play_events(iid, [("mapped", "v1", 1000.0), ("unmapped", "v2", 1000.0)])

    evidence = taste_viz.model_transparency(store, now=1000.0)["modes"]["evidence"]

    assert evidence["now"] == {"total": 2, "mapped": 1, "coverage": 0.5, "window_h": 6}
    assert evidence["session"]["mapped"] == 1


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


def test_saved_style_portrait_survives_a_content_space_change(store, monkeypatch):
    _install_now_modes(store)
    keys = ["a", "b", "c"]
    _install_now_content_vectors(store, monkeypatch, keys, np.array([[1.0, 0.0]] * 3))
    monkeypatch.setattr(embed, "content_space_id", lambda s, **kw: "new-content-space")
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.history.import_play_events(iid, [(k, "v" + k, 1000.0) for k in keys])

    ml = taste_viz.mode_layers(store, now=1000.0)

    assert not ml["styles_current"]
    assert ml["permanent"]["n"] == 80
    assert len(ml["permanent"]["segments"]) == 2
    assert ml["evidence"]["session"]["mapped"] == 3
    assert ml["now"]["segments"] == ml["session"]["segments"] == []


def test_generation_examples_read_prepared_pools_and_filter_hidden_tracks(store):
    store.rec_surface.put_proposals("mode_bundles", {"all": {"wheelhouse": [
        {"key": "hidden|artist", "title": "Hidden", "artist": "Artist"},
        {"key": "shown|artist", "title": "Shown", "artist": "Artist"},
    ]}}, now=1.0)
    store.rec_model.record_feedback("for_you", "hidden|artist", "dismiss", now=1.0)
    rec_params.set_param(store, "modes_artist_cap", 3)

    data = taste_viz.generated_playlists(store, now=2.0)

    assert data["examples"]["wheelhouse"]["title"] == "Shown"
    assert data["examples"]["fresh"] is None
    assert data["artist_cap"] == 3
    assert not data["taste_rank_ready"]


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
    monkeypatch.setattr(store.rec_query, "track_genres",
                        lambda ks: {"t|techno": "Techno", "j|jazz": "Jazz"})
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


def test_centroid_tilt_display_preserves_fading_magnitude(store, monkeypatch):
    _install_tilt_space(store, monkeypatch,
                        session=np.array([0.5, 0.0]), transient_dir=np.array([0.0, 0.125]))
    panel = taste_viz.centroid_tilt_panel(store, now=1000)
    fams = {f["name"]: f for f in panel["families"]}
    assert fams["techno"]["session"] == pytest.approx(0.5)
    assert fams["jazz"]["transient"] == pytest.approx(0.125)


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

def test_playlist_influence_preserves_whole_blend_and_listening_evidence(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    vectors, pids = [], []
    for i, (genre, plays) in enumerate((("Techno", 6), ("Jazz", 3), ("Folk", 1), ("Rock", 0))):
        tid = store.tracks.upsert_track(f"blend-{i}", f"Track {i}", "Artist", None, None)
        store.tracks.set_track_genre(tid, genre)
        pid = store.playlists.upsert_playlist(iid, f"blend-{i}", f"Playlist {i}", 1, f"blend-{i}", 0)
        store.playlists.set_playlist_tracks(pid, [tid])
        pids.append(pid)
        key = f"track {i}|artist"
        vectors.append((key, np.array([1, i + 1], dtype=np.float32).tobytes()))
        for j in range(plays):
            store.history.add_history_snapshot(iid, j * 86400.0, [key])
    store.rec_model.replace_rec_vectors(vectors)
    store.settings.set_setting("rec_dim", "2")

    panel = taste_viz.playlist_influence_panel(store, top=2)

    assert panel["basis"] == "plays"
    assert panel["contexts_total"] == 4 and panel["playlist_total"] == 3
    assert panel["unplayed_total"] == 1
    assert [c["pid"] for c in panel["contexts"]] == pids[:2]
    assert [c["listens"] for c in panel["contexts"]] == [6, 3]
    assert panel["contexts"][0]["genres"] == ["techno"]
    assert [c["percent_label"] for c in panel["segments"]] == ["60%", "30%", "10%"]
    assert [c["offset"] for c in panel["segments"]] == pytest.approx([0, 60, 90])
    assert sum(c["weight"] for c in panel["segments"]) == pytest.approx(1)
    assert panel["other"]["title"] == "Other playlist"
    assert panel["other"]["listens"] == 1


def test_playlist_influence_explains_equal_weights_before_listening(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    vectors = []
    for i in range(2):
        tid = store.tracks.upsert_track(f"equal-{i}", f"Track {i}", "Artist", None, None)
        pid = store.playlists.upsert_playlist(iid, f"equal-{i}", f"Playlist {i}", 1, f"equal-{i}", 0)
        store.playlists.set_playlist_tracks(pid, [tid])
        vectors.append((f"track {i}|artist", np.array([1, i + 1], dtype=np.float32).tobytes()))
    store.rec_model.replace_rec_vectors(vectors)
    store.settings.set_setting("rec_dim", "2")

    panel = taste_viz.playlist_influence_panel(store)

    assert panel["basis"] == "equal"
    assert [c["percent_label"] for c in panel["contexts"]] == ["50%", "50%"]
    assert all(c["listens"] == 0 and c["genres"] == [] for c in panel["contexts"])
    assert panel["other"] is None


def test_playlist_influence_identifies_behavior_fallback_as_listening(store):
    _seed_jazz(store)
    store.rec_model.replace_rec_vectors([("song|band", np.array([1, 0], dtype=np.float32).tobytes())])
    store.settings.set_setting("rec_dim", "2")

    panel = taste_viz.playlist_influence_panel(store)

    assert panel["basis"] == "listening" and panel["playlist_total"] == 0
    assert panel["contexts"][0]["title"] == "Your listening"
    assert panel["contexts"][0]["listens"] is None
    assert panel["contexts"][0]["percent_label"] == "100%"


def test_engine_panel_reports_counts_without_loading_playlist_model(store, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Technical setup must not load playlist taste")

    monkeypatch.setattr(taste_viz.scoring, "playlist_taste", unexpected)
    panel = taste_viz.engine_panel(store)
    assert panel["vectors"] == store.rec_model.rec_vectors_count()
    assert panel["dim"] >= 1


def test_breadth_word_thresholds():
    assert taste_viz._breadth_word(0.9) == "eclectic"
    assert taste_viz._breadth_word(0.5) == "balanced"
    assert taste_viz._breadth_word(0.1) == "focused"


def test_recent_play_counts_are_frequency_weighted(store):
    # Exact replay events count individually; repeated coarse captures of a day do not.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.tracks.upsert_track("v1", "Hit", "Star", None, None)
    store.tracks.upsert_track("v2", "Bsong", "Other", None, None)
    store.history.import_play_events(iid, [("hit|star", "v1", ts) for ts in (10.0, 20.0, 30.0)])
    store.history.import_play_events(iid, [("bsong|other", "v2", 40.0)])
    counts = store.history.recent_play_counts(1000)
    assert counts["hit|star"] == 3 and counts["bsong|other"] == 1
