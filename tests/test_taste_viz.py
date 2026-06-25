from yt_playlist.rec import rec_params, taste_viz


def _seed_jazz(store):
    """A jazz track that has been played, so it registers in the play-weighted genre distribution."""
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(t, "Jazz")
    store.add_history_snapshot(iid, 1.0, ["song|band"])
    return iid


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
    assert payload["sources"]["plays"] == 0
    assert payload["genres"] == []
    assert payload["freshness"]["live"] is True   # never-synced reads as fresh, not decayed


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


def test_stale_sync_reads_as_quiet(store):
    # Recent plays feed the transient model, but a long-stale sync decays every lean toward ~0.
    # The roses normalize to max-abs, so we must report 'quiet' rather than render a dead mood.
    iid = _seed_jazz(store)
    store.set_setting("last_sync_at", "100.0")
    assert taste_viz.model_transparency(store, now=100.0)["has_transient"] is True
    assert taste_viz.model_transparency(store, now=100.0 + 86400 * 40)["has_transient"] is False
