"""#114 missing enrichment is neutral rather than an accidental ranking penalty."""
import math

import pytest

from yt_playlist.rec import scoring


def _tracks(store):
    techno = store.upsert_track("t", "Techno", "A", None, None)
    folk = store.upsert_track("f", "Folk", "B", None, None)
    store.upsert_track("u", "Unknown", "C", None, None)
    store.set_track_genre(techno, "Techno")
    store.set_track_genre(folk, "Folk")
    return "techno|a", "folk|b", "unknown|c"


def test_missing_genre_sits_at_neutral_center_of_known_tilts(store):
    techno, folk, unknown = _tracks(store)
    store.set_weight("genre:techno", 2.0)

    mult, details = scoring._axis_weights_for(
        store, [techno, folk, unknown], with_details=True)

    assert mult[techno] == pytest.approx(math.sqrt(2))
    assert mult[folk] == pytest.approx(1 / math.sqrt(2))
    assert mult[unknown] == pytest.approx(1.0)
    assert details[unknown]["metadata_present"]["genre"] is False


def test_missing_genre_is_not_lifted_or_sunk_when_all_known_tracks_share_a_boost(store):
    techno, _folk, unknown = _tracks(store)
    store.set_weight("genre:techno", 2.0)
    mult = scoring._axis_weights_for(store, [techno, unknown])
    assert mult[techno] == pytest.approx(mult[unknown]) == pytest.approx(1.0)


def test_known_muted_genre_remains_a_hard_exclusion_beside_missing_genre(store):
    techno, folk, unknown = _tracks(store)
    store.set_weight("genre:techno", 0.0, lo=0.0, hi=2.0)
    mult = scoring._axis_weights_for(store, [techno, folk, unknown])
    assert mult[techno] == 0.0
    assert mult[unknown] == pytest.approx(1.0)
    assert mult[folk] == pytest.approx(1.0)


def test_explicit_genre_mute_does_not_mean_revert_into_a_soft_preference(store):
    store.set_weight("genre:techno", 0.0, lo=0.0, hi=2.0, now=1.0)
    assert store.get_weights(now=10_000_000.0)["genre:techno"] == 0.0
