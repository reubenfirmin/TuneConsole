"""Recipe storage (how a generated playlist was made) + per-day version numbering."""
from yt_playlist.library import playlist_metadata


def test_recipe_round_trips(store):
    store.rec_surface.set_recipe("PLX", {"model": "fresh", "facets": {"genres": ["house"], "eras": ["2010"]}}, now=1.0)
    got = store.rec_surface.get_recipe("PLX")
    assert got["model"] == "fresh" and got["facets"]["genres"] == ["house"]
    assert store.rec_surface.get_recipe("PLZ") is None                  # unknown playlist -> None


def test_versioned_title_increments_per_prefix(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pfx = "Fresh songs - June 21 2026"
    assert playlist_metadata.versioned_title(store, pfx) == f"{pfx} #1"
    store.playlists.upsert_playlist(iid, "P1", f"{pfx} #1", 0, "h", 0.0)
    assert playlist_metadata.versioned_title(store, pfx) == f"{pfx} #2"
    # a different type / day is independent
    assert playlist_metadata.versioned_title(store, "Comfort listening - June 21 2026") == "Comfort listening - June 21 2026 #1"
