"""Cached cards honor current feedback without banning generated playlist members.

Generated membership quarantines learning evidence. It does not suppress a song
that is independently eligible for recommendation.
"""
import numpy as np
import pytest
from tests.derived_rating_support import seed_dislike
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import native_sql_calls, without_query_timing

from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.rec import mode_surfaces as ms


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Cached-card SQL and ranking do not start the HTTP application."""


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
    owned = store.rec_query.tracks_by_keys(keys)
    seed_publication_subjects(store, [item for item in items if item['key'] not in owned])
    publish_bundles(store, payload, 1.0)


def _generated_playlist(store, keys):
    """Put `keys` into a playlist tagged as generated, the way create_generated_playlist does."""
    iid = store.accounts.upsert_account("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PLGEN", "More in your wheelhouse - #1", len(keys), "h", 1.0)
    tids = [store.conn.execute('SELECT e.id FROM catalog_entries e JOIN recordings r '
                              'ON r.id=e.recording_id WHERE r.identity_key=?', (key,)).fetchone()[0]
            for key in keys]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.set_playlist_group("PLGEN", GENERATED_GROUP)
    store.playlists.mark_generated_playlist('PLGEN')
    return pid


def _offered(cards):
    return {t["key"] for c in cards for t in c["tracks"]}


def test_generated_membership_preserves_independently_eligible_cached_candidates(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    _bundle(store, keys)
    assert _offered(ms.assemble_cards(store, now=10.0, epoch=0)), "no generated playlists: offer freely"
    bundled = keys[:6]
    _generated_playlist(store, bundled)
    offered = _offered(ms.assemble_cards(store, now=10.0, epoch=0))
    assert offered, "cards should still render"
    assert offered & set(bundled), "generated membership alone must not suppress candidates"
    assert offered & set(keys[6:]), "the untouched songs are still fair game"
    seed_dislike(store, bundled[0], until=None, now=11)
    assert bundled[0] not in _offered(ms.assemble_cards(store, now=12, epoch=0))


def test_cached_cards_honor_current_genre_mutes_without_rebuilding(store):
    keys = [f"song{i}|artist{i}" for i in range(20)]
    seed_publication_subjects(store, [
        dict(key=key, video_id=f'v{i}', title=key.split('|')[0], artist=key.split('|')[1],
             album='', duration_s=200, genre='House' if i < 6 else 'Techno')
        for i, key in enumerate(keys)])
    _bundle(store, keys)
    cached = store.rec_proposals.get_proposals("mode_bundles")
    assert _offered(ms.assemble_cards(store, now=10, epoch=0)) & set(keys[:6])

    store.rec_model.set_weight("genre:house", 0, lo=0, hi=2, now=10)
    offered = _offered(ms.assemble_cards(store, now=20, epoch=0))
    assert offered and offered <= set(keys[6:])
    assert store.rec_proposals.get_proposals("mode_bundles") == cached

    store.rec_model.set_weight("genre:house", 1, lo=0, hi=2, now=20)
    assert _offered(ms.assemble_cards(store, now=20, epoch=0)) & set(keys[:6])
