"""#50 rankability counts actual scores, separately from eligible metadata fallbacks."""
import pytest

from yt_playlist.rec import eval_recs, surfaces


def test_metadata_free_discovery_is_neither_eligible_nor_rankable(store):
    store.upsert_discovered_track("a|x", "v", "a", "x", "", None, None, None, "album", 0)
    assert surfaces.cold_candidates(store, 0) == []
    assert eval_recs.cold_rankable(store, 0) == {
        "rankable": 0, "fallback": 0, "eligible": 0, "pool": 1}


@pytest.mark.parametrize("genre,year,audio", [("Techno", None, {}), (None, "2000", {}),
                                              (None, None, {"bpm": 0}),
                                              (None, None, {"music_key": "C"})])
def test_metadata_without_model_is_eligible_but_not_rankable(store, genre, year, audio):
    store.upsert_discovered_track("a|x", "v", "a", "x", "", None, genre, year, "album", 0)
    if audio:
        store.set_discovered_audio("a|x", **audio)
    assert surfaces.cold_candidates(store, 0)[0].score is None
    assert eval_recs.cold_rankable(store, 0) == {
        "rankable": 0, "fallback": 1, "eligible": 1, "pool": 1}
