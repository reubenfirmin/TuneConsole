"""Cached cards honor current feedback without banning generated playlist members.

Generated membership quarantines learning evidence. It does not suppress a song
that is independently eligible for recommendation.
"""
import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.rec import mode_surfaces as ms


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _eye(i, d=4):
    v = np.zeros(d, dtype=np.float32)
    v[i] = 1.0
    return v


def _bundle(store, keys):
    store.modes.replace_modes(
        [{"mode_id": 1, "label": "m1", "families": [["house", 1]], "centroid": _eye(0),
          "size": 50, "rep_keys": []}], retired_ids=[], now=1.0)
    items = [{"key": k, "video_id": f"v{i}", "title": k.split("|")[0], "artist": k.split("|")[1],
              "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": "house"}
             for i, k in enumerate(keys)]
    payload = {"1": {s: list(items) for s in ms.CARD_SURFACES}}
    payload["_meta"] = {"comfort_pool": 100, "year_cuts": None}
    store.rec_proposals.put_proposals("mode_bundles", payload, 1.0)


def _generated_playlist(store, keys):
    """Put `keys` into a playlist tagged as generated, the way create_generated_playlist does."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PLGEN", "More in your wheelhouse - #1", len(keys), "h", 1.0)
    tids = [store.tracks.upsert_track(f"gv{i}", k.split("|")[0], k.split("|")[1], "Alb", 200)
            for i, k in enumerate(keys)]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.set_playlist_group("PLGEN", GENERATED_GROUP)
    store.playlists.mark_generated_playlist('PLGEN')
    return pid


def _offered(cards):
    return {t["key"] for c in cards for t in c["tracks"]}


def test_generated_membership_preserves_independently_eligible_cached_candidates(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    bundled = keys[:6]
    _generated_playlist(store, bundled)
    offered = _offered(ms.assemble_cards(store, now=10.0, epoch=0))
    assert offered, "cards should still render"
    assert offered & set(bundled), "generated membership alone must not suppress candidates"
    store.rec_model.record_dislike(bundled[0], until=None, now=11)
    assert bundled[0] not in _offered(ms.assemble_cards(store, now=12, epoch=0))


def test_cards_still_offer_everything_else(store):
    """Creating a generated playlist preserves other eligible candidates too."""
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    _generated_playlist(store, keys[:6])
    offered = _offered(ms.assemble_cards(store, now=10.0, epoch=0))
    assert offered & set(keys[6:]), "the untouched songs are still fair game"


def test_cards_are_unaffected_when_nothing_has_been_generated(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    assert _offered(ms.assemble_cards(store, now=10.0, epoch=0)), "no generated playlists: offer freely"


def test_cached_cards_honor_current_genre_mutes_without_rebuilding(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    for i, key in enumerate(keys):
        title, artist = key.split("|")
        tid = store.tracks.upsert_track(f"v{i}", title, artist, "", 200)
        store.tracks.set_track_genre(tid, "House" if i < 6 else "Techno")
    _bundle(store, keys)
    cached = store.rec_proposals.get_proposals("mode_bundles")
    assert _offered(ms.assemble_cards(store, now=10, epoch=0)) & set(keys[:6])

    store.rec_model.set_weight("genre:house", 0, lo=0, hi=2, now=10)
    offered = _offered(ms.assemble_cards(store, now=20, epoch=0))
    assert offered and offered <= set(keys[6:])
    assert store.rec_proposals.get_proposals("mode_bundles") == cached

    store.rec_model.set_weight("genre:house", 1, lo=0, hi=2, now=20)
    assert _offered(ms.assemble_cards(store, now=20, epoch=0)) & set(keys[:6])
