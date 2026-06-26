"""#43 / #38 §4b: every suggestion dismiss reason steers the taste model THROUGH the graduation
ledger (not a direct nudge_weight), and 'too mainstream' drives a real popularity axis the scorer
applies. The tell that steering went through the funnel: a single event moves the rec_theme ledger
but leaves the permanent weight neutral (it is below THEME_THRESHOLD), whereas the old direct
nudge_weight moved the weight immediately."""
from fastapi.testclient import TestClient

from yt_playlist.rec import rec_params, scoring
from yt_playlist.util import genre_map
from yt_playlist.web.app import create_app


def _post(store, now=1000.0, **data):
    c = TestClient(create_app(store, lambda: {}, now_fn=lambda: now), base_url="http://127.0.0.1")
    return c.post("/recs/feedback", data=data)


def test_wrong_vibe_steers_genre_through_the_ledger_not_a_direct_nudge(store):
    store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    r = _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="vibe")
    assert r.status_code == 200
    assert (store.get_theme(f"genre:{fam}") or 0) < 0               # accumulated in the ledger
    assert store.get_weights().get(f"genre:{fam}", 1.0) == 1.0      # not nudged directly (sub-threshold)


def test_wrong_era_steers_the_decade(store):
    store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_year(tid, "1995")
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="era")
    assert (store.get_theme("era:1990") or 0) < 0


def test_not_this_artist_steers_artist_through_the_ledger(store):
    store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Song", "Band", None, None)
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss",
          reason="artist", axis="artist:Band")                     # the template sends an explicit axis
    assert (store.get_theme("artist:Band") or 0) < 0
    assert store.get_weights().get("artist:Band", 1.0) == 1.0      # routed through the ledger, not nudged


def test_already_know_it_suppresses_without_any_taste_steer(store):
    store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("v1", "Song", "Band", None, None)
    store.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="own_it")
    assert not store.get_theme(f"genre:{fam}")                      # no taste penalty
    assert "song|band" in store.suppressed_keys("suggest", now=1001.0, scope="7")   # but suppressed


def test_too_mainstream_steers_pop_only_when_track_is_mainstream(store):
    store.upsert_identity("main", "cred", None, True)
    thresh = int(rec_params.get_param(store, "pop_mainstream_min"))
    hot = store.upsert_track("v1", "Hot", "A", None, None)
    store.set_track_audio(hot, popularity=thresh + 1000)
    cold = store.upsert_track("v2", "Cold", "B", None, None)
    store.set_track_audio(cold, popularity=1)
    _post(store, item="hot|a", surface="suggest", scope="7", kind="dismiss", reason="mainstream")
    assert (store.get_theme("pop:mainstream") or 0) < 0
    before = store.get_theme("pop:mainstream")
    _post(store, item="cold|b", surface="suggest", scope="7", kind="dismiss", reason="mainstream")
    assert store.get_theme("pop:mainstream") == before             # a niche track doesn't steer mainstream


def test_why_chip_axis_routes_through_graduation_not_direct_nudge(store):
    store.upsert_identity("main", "cred", None, True)
    _post(store, item="x|y", surface="for_you", kind="more", axis="genre:techno")
    assert (store.get_theme("genre:techno") or 0) > 0
    assert store.get_weights().get("genre:techno", 1.0) == 1.0     # the §4b leak is closed


def test_pop_band_scorer_downweights_mainstream_tracks(store):
    thresh = int(rec_params.get_param(store, "pop_mainstream_min"))
    hot = store.upsert_track("v1", "Hot", "A", None, None)
    store.set_track_audio(hot, popularity=thresh + 1000)
    cold = store.upsert_track("v2", "Cold", "B", None, None)
    store.set_track_audio(cold, popularity=1)
    store.set_weight("pop:mainstream", 0.5)                         # steered away from mainstream
    mult = scoring._axis_weights_for(store, ["hot|a", "cold|b"])
    assert mult is not None
    assert mult["hot|a"] < mult["cold|b"]                          # the mainstream track ranks lower


def test_vibe_on_untagged_track_is_a_graceful_noop(store):
    store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Song", "Band", None, None)           # no genre
    r = _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="vibe")
    assert r.status_code == 200                                    # no axis derived -> no steer, no error
