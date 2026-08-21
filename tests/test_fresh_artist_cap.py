# tests/test_fresh_artist_cap.py
"""The Fresh card offers a spread of new music, not one artist's back catalogue.

Discovery walks whole discographies, so its pool is lumpy by construction - measured on a real
library, 464 pooled tracks with Muse 48 / Shpongle 34 / Underworld 31 of them. A plain taste ranking
over that returned nine Underworld tracks in a twelve-track card.
"""
from collections import Counter

from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params
from yt_playlist.rec.surfaces import _cap_per_artist


def _scored(spec):
    """(score, row) pairs as cold_candidates builds them, best first."""
    return [(1.0 - i / 1000, {"artist": a, "title": t, "identity_key": f"{t}|{a}".lower()})
            for i, (t, a) in enumerate(spec)]


def _artists(pairs, n=12):
    return Counter(r["artist"] for _, r in pairs[:n])


def test_one_artist_cannot_fill_the_card():
    pool = _scored([(f"Track {i}", "Underworld") for i in range(9)] +
                   [(f"Other {i}", f"Artist {i}") for i in range(20)])

    got = _cap_per_artist(pool, 2)

    assert _artists(got)["Underworld"] == 2
    assert len(_artists(got)) >= 10          # a spread, not a back catalogue


def test_the_best_tracks_of_a_capped_artist_are_the_ones_kept():
    pool = _scored([("Best", "Underworld"), ("Second", "Underworld"), ("Third", "Underworld")] +
                   [(f"Other {i}", f"Artist {i}") for i in range(10)])

    kept = [r["title"] for _, r in _cap_per_artist(pool, 2)[:12] if r["artist"] == "Underworld"]

    assert kept == ["Best", "Second"]


def test_a_thin_pool_still_fills_the_card():
    """Held-back tracks are appended, not dropped: two artists shouldn't yield a four-track card.
    The cap bends only once the alternatives are exhausted."""
    pool = _scored([(f"A{i}", "Only Artist") for i in range(8)] +
                   [(f"B{i}", "Other Artist") for i in range(8)])

    got = _cap_per_artist(pool, 2)

    assert len(got) == 16                     # nothing lost
    assert len(got[:12]) == 12                # and the card still fills


def test_an_accented_spelling_does_not_get_its_own_allowance():
    """YouTube credits the same act both ways; an exact-string cap would let each spelling through."""
    pool = _scored([("A", "Einstürzende Neubauten"), ("B", "Einsturzende Neubauten"),
                    ("C", "Einstürzende Neubauten"), ("D", "Someone Else")])

    got = _cap_per_artist(pool, 2)[:3]

    assert sum(1 for _, r in got if "eubauten" in r["artist"]) == 2


def test_the_cap_is_tunable_and_defaults_to_two():
    s = Store(":memory:")
    s.init_schema()
    assert rec_params.get_param(s, "fresh_artist_cap") == 2
    spec = rec_params.PARAMS_BY_NAME["fresh_artist_cap"]
    assert spec.integer and spec.min >= 1
