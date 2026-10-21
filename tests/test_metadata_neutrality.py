"""#114 missing enrichment is neutral rather than an accidental ranking penalty."""
import math

import numpy as np
import pytest

from yt_playlist.rec import scoring, surfaces


def _tracks(store):
    techno = store.tracks.upsert_track("t", "Techno", "A", None, None)
    folk = store.tracks.upsert_track("f", "Folk", "B", None, None)
    store.tracks.upsert_track("u", "Unknown", "C", None, None)
    store.tracks.set_track_genre(techno, "Techno")
    store.tracks.set_track_genre(folk, "Folk")
    return "techno|a", "folk|b", "unknown|c"


def test_missing_genre_sits_at_neutral_center_of_known_tilts(store):
    techno, folk, unknown = _tracks(store)
    store.rec_model.set_weight("genre:techno", 2.0)

    mult, details = scoring._axis_weights_for(
        store, [techno, folk, unknown], with_details=True)

    assert mult[techno] == pytest.approx(math.sqrt(2))
    assert mult[folk] == pytest.approx(1 / math.sqrt(2))
    assert mult[unknown] == pytest.approx(1.0)
    assert details[unknown]["metadata_present"]["genre"] is False


def test_missing_genre_is_not_lifted_or_sunk_when_all_known_tracks_share_a_boost(store):
    techno, _folk, unknown = _tracks(store)
    store.rec_model.set_weight("genre:techno", 2.0)
    mult = scoring._axis_weights_for(store, [techno, unknown])
    assert mult[techno] == pytest.approx(mult[unknown]) == pytest.approx(1.0)


def test_known_muted_genre_remains_a_hard_exclusion_beside_missing_genre(store):
    techno, folk, unknown = _tracks(store)
    store.rec_model.set_weight("genre:techno", 0.0, lo=0.0, hi=2.0)
    mult = scoring._axis_weights_for(store, [techno, folk, unknown])
    assert mult[techno] == 0.0
    assert mult[unknown] == pytest.approx(1.0)
    assert mult[folk] == pytest.approx(1.0)


def test_explicit_genre_mute_does_not_mean_revert_into_a_soft_preference(store):
    store.rec_model.set_weight("genre:techno", 0.0, lo=0.0, hi=2.0, now=1.0)
    assert store.rec_model.get_weights(now=10_000_000.0)["genre:techno"] == 0.0


@pytest.mark.parametrize("scorer", [surfaces.for_you, surfaces.explore_for_you,
                                   surfaces.comfort_listening])
def test_genre_mute_excludes_owned_recommendations_until_manual_unmute(store, scorer):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    techno, folk, unknown = _tracks(store)
    tids = [row["id"] for row in store.conn.execute("SELECT id FROM tracks ORDER BY id")]
    pid = store.playlists.upsert_playlist(iid, "PL", "Listening", 3, "h", 0)
    store.playlists.set_playlist_tracks(pid, tids)
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], dtype=np.float32).tobytes())
                               for key in (techno, folk, unknown)])
    for day in range(4):
        for key in (techno, folk, unknown):
            store.history.record_history_plays(iid, day * 86400, [(key, "Today")])
    now = 100 * 86400
    before = {item.key for item in scorer(store, now)}
    assert techno in before and unknown in before

    store.rec_model.set_weight("genre:techno", 0.0, lo=0, hi=2, now=now)
    muted = {item.key for item in scorer(store, now + 86400)}
    assert techno not in muted and unknown in muted
    inspected = next(row for row in surfaces.taste_comparison(store, now + 86400, keys=[techno])
                     if row["key"] == techno)
    assert inspected["eligible"] is False and inspected["eligibility_reason"] == "genre muted"

    store.rec_model.set_weight("genre:techno", 1.0, lo=0, hi=2, now=now + 86400)
    assert techno in {item.key for item in scorer(store, now + 86400)}


@pytest.mark.parametrize("axis", ["genre:house", "genre:deep house"])
def test_genre_mutes_suppress_library_and_discovery_keys_without_hiding_unknowns(store, axis):
    known = store.tracks.upsert_track("known", "Known", "Artist", None, None)
    store.tracks.set_track_genre(known, "Deep House")
    store.tracks.upsert_track("unknown", "Unknown", "Artist", None, None)
    store.discovery.upsert_discovered_track("cold|artist", "cold", "Cold", "Artist", "", None,
                                  "Deep House", "2000", "source", 0)
    store.rec_model.set_weight(axis, 0, lo=0, hi=2, now=1)
    for surface in ("for_you", "suggest"):
        assert store.rec_model.suppressed_keys(surface, 100 * 86400) == {"known|artist", "cold|artist"}
    store.rec_model.reset_weights()
    assert store.rec_model.suppressed_keys("for_you", 100 * 86400) == set()
