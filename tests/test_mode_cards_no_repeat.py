"""Generated playlists must not re-offer songs already bundled into one.

The mode bundles apply the generated-track exclusion when they are BUILT, but creating a playlist
does not rebuild them. Every card rendered between one build and the next therefore kept offering the
songs you had just bundled, so generating again that day handed back the same tracks. Verified on a
real library: bundles built 05:24, and the cards were still offering 33 songs (15 wheelhouse, 17
explore, 1 temporal) that were already in that day's two generated playlists.

The exclusion has to be a fact at RENDER time, not a snapshot taken at build time.
"""
import numpy as np
import pytest

from yt_playlist.core.store import Store
from yt_playlist.library.executor import GENERATED_GROUP
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
    store.put_proposals("mode_bundles", payload, 1.0)


def _generated_playlist(store, keys):
    """Put `keys` into a playlist tagged as generated, the way create_generated_playlist does."""
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLGEN", "More in your wheelhouse - #1", len(keys), "h", 1.0)
    tids = [store.upsert_track(f"gv{i}", k.split("|")[0], k.split("|")[1], "Alb", 200)
            for i, k in enumerate(keys)]
    store.set_playlist_tracks(pid, tids)
    store.set_playlist_group("PLGEN", GENERATED_GROUP)
    return pid


def _offered(cards):
    return {t["key"] for c in cards for t in c["tracks"]}


def test_cards_do_not_reoffer_songs_already_in_a_generated_playlist(store):
    """The reported bug: generate a playlist, then the very next card hands the same songs back."""
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    bundled = keys[:6]
    _generated_playlist(store, bundled)
    offered = _offered(ms.assemble_cards(store, now=10.0, epoch=0))
    assert offered, "cards should still render"
    assert not (offered & set(bundled)), "already bundled into a generated playlist, must not return"


def test_cards_still_offer_everything_else(store):
    """The exclusion must not gut the cards: only the bundled songs go."""
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    _generated_playlist(store, keys[:6])
    offered = _offered(ms.assemble_cards(store, now=10.0, epoch=0))
    assert offered & set(keys[6:]), "the untouched songs are still fair game"


def test_cards_are_unaffected_when_nothing_has_been_generated(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    assert _offered(ms.assemble_cards(store, now=10.0, epoch=0)), "no generated playlists: offer freely"
