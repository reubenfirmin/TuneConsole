"""#63: date cards use the actual library's release years, without listening history."""
from contextlib import ExitStack

import numpy as np
import pytest

from yt_playlist.core import catalog
from yt_playlist.rec import mode_surfaces as ms, rec_params, surfaces
from yt_playlist.util.matching import identity_key


def _library(store, monkeypatch, years, *, modes=1):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    tracks, vectors, membership = [], [], {}
    # The repository methods delegate to these same catalog operations, but
    # commit after each track/field. This builder owns one real transaction.
    with store._lock, store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        for mid in range(1, modes + 1):
            direction = np.eye(modes, dtype=np.float32)[mid - 1]
            for i, year in enumerate(years):
                title, artist = f"Release {mid} {i}", f"Artist {mid} {i}"
                tid = catalog.upsert(store.conn, f"v{mid}_{i}", title, artist, f"Album {mid} {i}", 200,
                                     library=True)
                catalog.choose_field(store.conn, tid, 'genre', "house" if mid == 1 else "techno")
                if year is not None:
                    catalog.choose_field(store.conn, tid, 'mb_year', str(year))
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
    _temporal_fallback(monkeypatch)
    return membership


def _temporal_fallback(monkeypatch):
    # Preserve the original four seams; dates, vectors, buckets and assembly are real.
    for name in ("for_you", "explore_for_you", "comfort_listening"):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *args, **kwargs: [])


@pytest.fixture(scope='module')
def temporal_templates(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template

    with ExitStack() as resources:
        templates = {}

        def template(years, *, prepared=True):
            # Track ordering matters to the full-page regression. Do not merge
            # alternating and blocked years merely because their counts match.
            key = (tuple(years), prepared)
            if key not in templates:
                def build(source):
                    with pytest.MonkeyPatch.context() as patch:
                        membership = _library(source, patch, key[0], modes=2)
                        payload = ms.prepare_bundles(source, now=1000) if prepared else None
                    return membership, payload

                templates[key] = resources.enter_context(
                    prepared_template(_pristine_current_schema, build))
            return templates[key]

        yield template


@pytest.fixture
def temporal_library(store, monkeypatch, temporal_templates):
    _temporal_fallback(monkeypatch)

    def restore(years, *, prepared=True):
        return temporal_templates(years, prepared=prepared).restore(store)

    return restore


@pytest.mark.parametrize("years", [(1970, 1995, 2020), (2021, 2023, 2025)])
def test_computed_bands_rotate_relative_to_library(store, temporal_library, years):
    membership, payload = temporal_library(list(years) * 40)
    cuts = payload["_meta"]["year_cuts"]
    assert years[0] <= cuts[0] < years[1] <= cuts[1] < years[2]
    with store.read_view() as reader:
        head = reader.recommendations.head("home")
        assert head["status"] == "published"
        framing = reader.recommendations.framing("home", generation=head["id"])
        assert framing["generation_id"] == head["id"]
        assert framing["presence"] & 2
        assert [framing["year_low"], framing["year_high"]] == cuts

    for epoch, (year, label) in enumerate(zip(years, ("Throwback", "Time Flies", "Recent Picks"))):
        cards = ms.assemble_cards(store, now=1000, epoch=epoch)
        assert len(cards) == 1
        card = cards[0]
        assert card["lane"] == "temporal" and card["label"] == label
        assert len(card["tracks"]) == ms.PROTO_SIZE
        assert {track["year"] for track in card["tracks"]} == {year}
        assert {membership[track["key"]] for track in card["tracks"]} == {card["mode_id"]}
        assert {track["serving_context"]["recommendation_generation_id"]
                for track in card["tracks"]} == {head["id"]}
    assert store.history.play_events_since(0) == []


@pytest.mark.parametrize("epoch,year", [(1, 1995), (2, 2020)])
def test_credible_date_bands_survive_a_bucket_full_of_older_tracks(store, temporal_library, epoch, year):
    membership, _payload = temporal_library([1970] * 40 + [1995] * 40 + [2020] * 40)
    cards = ms.assemble_cards(store, now=1000, epoch=epoch)
    assert len(cards) == 1, f"The full library has 40 releases per mode from {year}"
    assert {track["year"] for track in cards[0]["tracks"]} == {year}
    assert len(cards[0]["tracks"]) == ms.PROTO_SIZE
    assert {membership[track["key"]] for track in cards[0]["tracks"]} == {cards[0]["mode_id"]}


@pytest.mark.parametrize("cap", [12, 40])
def test_each_date_band_keeps_candidates_past_newer_and_unknown_releases(store, temporal_library, cap):
    membership, unprepared = temporal_library(
        [None] * 60 + [2020] * 60 + [1995] * 60 + [1970] * 60, prepared=False)
    assert unprepared is None
    with store.read_view() as reader:
        assert reader.recommendations.head('home') is None
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


def test_temporal_template_restores_private_catalog_vectors_payload_and_files(
        store, file_backed_store, temporal_templates):
    from tests.prepared_fixture_support import roots

    template = temporal_templates([1970, 1995, 2020] * 40)
    membership, payload = template.restore(store)
    other_membership, other_payload = template.restore(file_backed_store)
    assert len(membership) == len(other_membership) == 240
    assert set(membership.values()) == {1, 2}
    assert (membership, payload) == (other_membership, other_payload) == template.result
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    sql = 'SELECT id,video_id,title,artist,genre,mb_year FROM catalog_tracks ORDER BY id'
    expected = [tuple(row) for row in template.source.conn.execute(sql)]
    vectors = template.source.rec_model.get_rec_content_vectors()
    assert len(vectors) == 240
    for private in (store, file_backed_store):
        assert [tuple(row) for row in private.conn.execute(sql)] == expected
        assert private.rec_model.get_rec_content_vectors() == vectors
        assert private.history.play_events_since(0) == []

    first_roots, second_roots = roots(store.conn), roots(file_backed_store.conn)
    assert set(first_roots.values()).isdisjoint(second_roots.values())
    assert set(first_roots.values()).isdisjoint(template.files.values())
    assert set(second_roots.values()).isdisjoint(template.files.values())
    retained = []
    for name, root in template.files.items():
        for source in root.rglob('*'):
            if not source.is_file():
                continue
            first = first_roots[name] / source.relative_to(root)
            second = second_roots[name] / source.relative_to(root)
            original = source.read_bytes()
            assert first.read_bytes() == second.read_bytes() == original
            assert len({(path.stat().st_dev, path.stat().st_ino) for path in (source, first, second)}) == 3
            retained.append((source, first, second, original))
    assert retained
    membership.clear()
    payload['_meta']['year_cuts'][0] = -1
    store.tracks.set_track_title(expected[0][0], 'Private temporal catalog mutation')
    assert [tuple(row) for row in store.conn.execute(sql)] != expected
    store.rec_model.replace_rec_content_vectors([(key, bytes(len(blob))) for key, blob in vectors])
    assert store.rec_model.get_rec_content_vectors() != vectors
    retained[0][1].write_bytes(b'private temporal file mutation')
    assert (other_membership, other_payload) == template.result
    for unchanged in (file_backed_store, template.source):
        assert [tuple(row) for row in unchanged.conn.execute(sql)] == expected
        assert unchanged.rec_model.get_rec_content_vectors() == vectors
    for source, _first, second, original in retained:
        assert source.read_bytes() == second.read_bytes() == original
