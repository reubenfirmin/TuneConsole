"""Home policy and effect boundaries can be exercised without an HTTP request."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conftest import seed_catalog_track
from tests.repository_mock_support import no_database, transient_repository_store
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.rec import home_cards, measurement, mode_surfaces, stored_home


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Home policy tests do not construct the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


class Jobs:
    def __init__(self):
        self.admitted = []

    def latest(self, kind, parameters):
        return None

    def start(self, kind, parameters, excluded):
        self.admitted.append((kind, parameters, excluded))
        return SimpleNamespace(kind=kind, parameters=parameters, exclude_keys=excluded,
                               done=False, error=None, events=[])


def service(store):
    return home_cards.HomeCards(store, Jobs(), logging.getLogger(__name__))


def proto(lane="wheelhouse"):
    return {"lane": lane, "label": "Mix", "tracks": [{"key": "a|b", "title": "A", "artist": "B"}],
            "recipe": {}}


@pytest.fixture
def home_store(no_database):
    return transient_repository_store(
        rec_ledger={'card_views': 0},
        rec_proposals={'group_checkpoint': None},
        recommendations={'head': None, 'framing': None},
        rec_query={'artist_genre_years': {}, 'artist_genre_years_for': {},
                   'genre_play_distribution': {}, 'era_play_distribution': {}},
        modes={'log_impressions': None},
        discovery={'mark_offered': None},
        values={'_recommendation_files': True},
    )


def test_empty_menu_has_four_buildable_slots_and_does_not_admit_work(home_store):
    home = service(home_store)
    result = home.cards(home_cards.CardsRequest(1000))
    assert result.protos == []
    assert [slot["pool"]["lane"] for slot in result.slots] == ["wheelhouse", "explore", "fresh", "comfort"]
    assert all(not slot["pool"]["building"] for slot in result.slots)
    assert home.jobs.admitted == []


def test_other_genre_fallback_returns_usable_mix_without_track_exposure(home_store, monkeypatch):
    request = {"epoch": 0, "avoid_genres": ["jazz"]}
    home_store.rec_proposals.get_proposals.side_effect = (
        lambda key: request if key == 'home_genre_request' else {})
    home = service(home_store)
    searches = []

    def candidates(store, now, **filters):
        searches.append(filters)
        return [] if filters.get('avoid') else [proto()]

    monkeypatch.setattr(stored_home, "unmodeled_cards", candidates)
    no_track_exposure = Mock(side_effect=AssertionError("A mix menu does not expose its individual tracks"))
    monkeypatch.setattr(measurement, "record_offers", no_track_exposure)
    home_store.discovery.mark_offered.side_effect = AssertionError("No tracks were served")
    result = home.cards(home_cards.CardsRequest(1000))
    assert "usual mixes" in result.genre_warning
    assert any(search.get("avoid") == {"jazz"} for search in searches)
    assert [track['key'] for track in result.protos[0]['tracks']] == ['a|b']
    no_track_exposure.assert_not_called()
    home_store.discovery.mark_offered.assert_not_called()
    assert home.jobs.admitted == []


def test_chosen_genres_reach_ranker_and_stay_on_final_recipe(home_store, monkeypatch):
    request = {"epoch": 0, "include_genres": ["jazz"]}
    home_store.rec_proposals.get_proposals.side_effect = (
        lambda key: request if key == 'home_genre_request' else {})
    home = service(home_store)
    calls = []

    def assemble(store, now, epoch, **filters):
        calls.append((now, epoch, filters))
        return [{**proto(), "mode_id": 7, "ranker": "lane"}]

    monkeypatch.setattr(mode_surfaces, "assemble_cards", assemble)
    monkeypatch.setattr(home_cards, "_order_by_journey", lambda store, tracks, journey, seed, data=None: tracks)
    result = home.cards(home_cards.CardsRequest(1000))
    assert calls == [(1000, 0, {"include_genres": {"jazz"}})]
    assert result.requested_genres == ["jazz"]
    assert result.protos[0]["recipe"]["genre_request"] == {"kind": "chosen_genres", "include": ["jazz"]}
    assert result.protos[0]["recipe"]["ranker"] == "lane"
    home_store.modes.log_impressions.assert_called_once_with(0, [('wheelhouse', 7, 'lane')], 1000)


def test_pool_failure_and_polling_do_not_admit_jobs_or_record_offers(home_store):
    home = service(home_store)
    parameters = {"include_genres": [], "avoid_genres": []}
    job = home.start_pool("fresh_pool", parameters, [])
    assert home.jobs.admitted == [("fresh_pool", parameters, [])]
    assert home.pool(job, 1000, "/home/fresh-pool/1").pool["building"]
    job.done, job.error = True, "Provider unavailable"
    result = home.pool(job, 1000, "/home/fresh-pool/1")
    assert result.proto is None
    assert result.pool["message"] == "Fresh mix not completed. You can try again."
    assert job.error == "Provider unavailable"
    assert not result.pool["building"]
    assert len(home.jobs.admitted) == 1
    home_store.discovery.mark_offered.assert_not_called()


def test_catalog_excludes_only_displayed_wheelhouse_tracks(store, monkeypatch):
    # Two complete 12-track menus plus an undisplayed candidate distinguish
    # displayed-track exclusion from excluding the whole wheelhouse pool.
    for i in range(25):
        title, artist = f"Song {i}", f"Artist {i}"
        seed_catalog_track(store, f'song {i}|artist {i}', title, artist, video_id=f'v{i}')
    from yt_playlist.rec import measurement
    shared = [{'key': f'song {i}|artist {i}'} for i in range(25)]
    with store.read_snapshot() as snapshot:
        policies = measurement.PoolPolicies(snapshot.store, 999)
        pool = policies.stamp(shared, 'wheelhouse')
        assert mode_surfaces._publish_typed_bundles(store, snapshot,
            {'all': {'wheelhouse': pool, 'explore': pool}, '_meta': {'unmodeled': True}},
            [], policies, {}, 999)

    result = service(store).cards(home_cards.CardsRequest(1000))
    by_lane = {p["lane"]: p["tracks"] for p in result.protos}

    wheelhouse = {t['key'] for t in by_lane["wheelhouse"]}
    catalog = {t['key'] for t in by_lane["explore"]}
    assert len(wheelhouse) == len(catalog) == 12
    assert wheelhouse.isdisjoint(catalog)
