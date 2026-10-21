"""#63: date cards use the actual library's release years, without listening history."""
import numpy as np
import pytest

from yt_playlist.rec import mode_surfaces as ms, rec_params, surfaces
from yt_playlist.util.matching import identity_key


def _library(store, monkeypatch, years, *, modes=1):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tracks, vectors, membership = [], [], {}
    for mid in range(1, modes + 1):
        direction = np.eye(modes, dtype=np.float32)[mid - 1]
        for i, year in enumerate(years):
            title, artist = f"Release {mid} {i}", f"Artist {mid} {i}"
            tid = store.tracks.upsert_track(f"v{mid}_{i}", title, artist, f"Album {mid} {i}", 200)
            store.tracks.set_track_genre(tid, "house" if mid == 1 else "techno")
            if year is not None:
                store.tracks.set_track_year(tid, str(year))
            key = identity_key(title, artist)
            vectors.append((key, direction.tobytes()))
            membership[key] = mid
            tracks.append(tid)
    pid = store.playlists.upsert_playlist(iid, "PL_library", "Library", len(tracks), "h", 0)
    store.playlists.set_playlist_tracks(pid, tracks)
    store.rec_model.replace_rec_content_vectors(vectors)
    store.modes.replace_modes([
        {"mode_id": mid, "label": f"Mode {mid}", "families": [["house" if mid == 1 else "techno", 1]],
         "centroid": np.eye(modes, dtype=np.float32)[mid - 1], "size": len(years), "rep_keys": []}
        for mid in range(1, modes + 1)
    ], retired_ids=[], now=1)
    # Isolate the temporal fallback. Its years, vectors, buckets and assembly remain real.
    for name in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *args, **kwargs: [])
    return membership


@pytest.mark.parametrize("years", [(1970, 1995, 2020), (2021, 2023, 2025)])
def test_computed_bands_rotate_relative_to_library(store, monkeypatch, years):
    membership = _library(store, monkeypatch, list(years) * 40, modes=2)
    payload = ms.prepare_bundles(store, now=1000)
    cuts = payload["_meta"]["year_cuts"]
    assert years[0] <= cuts[0] < years[1] <= cuts[1] < years[2]
    assert store.rec_surface.get_proposals("mode_bundles")["_meta"]["year_cuts"] == cuts

    for epoch, (year, label) in enumerate(zip(years, ("Throwback", "Time Flies", "Recent Picks"))):
        cards = ms.assemble_cards(store, now=1000, epoch=epoch)
        assert len(cards) == 1
        card = cards[0]
        assert card["lane"] == "temporal" and card["label"] == label
        assert len(card["tracks"]) == ms.PROTO_SIZE
        assert {track["year"] for track in card["tracks"]} == {year}
        assert {membership[track["key"]] for track in card["tracks"]} == {card["mode_id"]}
    assert store.history.play_events_since(0) == []


@pytest.mark.parametrize("epoch,year", [(1, 1995), (2, 2020)])
def test_credible_date_bands_survive_a_bucket_full_of_older_tracks(store, monkeypatch, epoch, year):
    membership = _library(store, monkeypatch, [1970] * 40 + [1995] * 40 + [2020] * 40, modes=2)
    ms.prepare_bundles(store, now=1000)
    cards = ms.assemble_cards(store, now=1000, epoch=epoch)
    assert len(cards) == 1, f"The full library has 40 releases per mode from {year}"
    assert {track["year"] for track in cards[0]["tracks"]} == {year}
    assert len(cards[0]["tracks"]) == ms.PROTO_SIZE
    assert {membership[track["key"]] for track in cards[0]["tracks"]} == {cards[0]["mode_id"]}


@pytest.mark.parametrize("cap", [12, 40])
def test_each_date_band_keeps_candidates_past_newer_and_unknown_releases(store, monkeypatch, cap):
    membership = _library(store, monkeypatch,
                          [None] * 60 + [2020] * 60 + [1995] * 60 + [1970] * 60, modes=2)
    rec_params.set_param(store, "modes_cand_per_mode", cap)
    payload = ms.prepare_bundles(store, now=1000)

    # Each rotating card gets the configured candidate depth; unknown years cannot spend it.
    for mid in (1, 2):
        candidates = payload[str(mid)]["temporal"]
        assert len(candidates) == 3 * cap
        for year in (1970, 1995, 2020):
            assert sum(track["year"] == year for track in candidates) == cap
        assert {membership[track["key"]] for track in candidates} == {mid}
    assert len(payload["all"]["temporal"]) == 480

    for epoch, year in enumerate((1970, 1995, 2020)):
        cards = ms.assemble_cards(store, now=1000, epoch=epoch)
        assert len(cards) == 1
        card = cards[0]
        assert len(card["tracks"]) == ms.PROTO_SIZE
        assert {track["year"] for track in card["tracks"]} == {year}
        assert len({track["artist"] for track in card["tracks"]}) >= 6
        assert {membership[track["key"]] for track in card["tracks"]} == {card["mode_id"]}
    assert store.history.play_events_since(0) == []


def test_missing_years_do_not_make_a_sparse_library_credible(store, monkeypatch):
    _library(store, monkeypatch, [1990] * 29 + [None] * 50)
    payload = ms.prepare_bundles(store, now=1000)
    assert len(payload["all"]["temporal"]) == 79
    assert payload["_meta"]["year_cuts"] is None
    assert all(ms.assemble_cards(store, now=1000, epoch=epoch) == [] for epoch in range(3))


def test_single_year_catalog_omits_empty_bands_and_unknown_dates(store, monkeypatch):
    _library(store, monkeypatch, [2024] * 40 + [None] * 20)
    payload = ms.prepare_bundles(store, now=1000)
    assert payload["_meta"]["year_cuts"] == [2024, 2024]
    cards = ms.assemble_cards(store, now=1000, epoch=0)
    assert len(cards) == 1 and len(cards[0]["tracks"]) == ms.PROTO_SIZE
    assert {track["year"] for track in cards[0]["tracks"]} == {2024}
    assert ms.assemble_cards(store, now=1000, epoch=1) == []
    assert ms.assemble_cards(store, now=1000, epoch=2) == []
