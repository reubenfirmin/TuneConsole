"""Home policy and effect boundaries can be exercised without an HTTP request."""
import logging
from types import SimpleNamespace

from yt_playlist.rec import home_cards, mode_surfaces, surfaces


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


def test_empty_menu_has_four_buildable_slots_and_does_not_admit_work(store):
    home = service(store)
    result = home.cards(home_cards.CardsRequest(1000))
    assert result.protos == []
    assert [slot["pool"]["lane"] for slot in result.slots] == ["wheelhouse", "explore", "fresh", "comfort"]
    assert all(not slot["pool"]["building"] for slot in result.slots)
    assert home.jobs.admitted == []


def test_other_genre_fallback_records_only_final_offer_once(store, monkeypatch):
    store.rec_surface.put_proposals("home_genre_request", {"epoch": 0, "avoid_genres": ["jazz"]}, 1000)
    home = service(store)
    offered, served, searches = [], [], []

    def candidates(store, lane, now, **filters):
        searches.append(filters)
        return proto() if not filters and lane == "wheelhouse" else None

    monkeypatch.setattr(home_cards, "_one_card", candidates)
    monkeypatch.setattr(home_cards, "_record_served_cards", lambda store, protos, now: served.append(protos))
    monkeypatch.setattr(store.discovery, "mark_offered", lambda kind, keys, now: offered.append(keys))
    result = home.cards(home_cards.CardsRequest(1000))
    assert "usual mixes" in result.genre_warning
    assert any(search.get("avoid_genres") == {"jazz"} for search in searches)
    assert offered == [["a|b"]]
    assert served == [[result.protos[0]]]
    assert home.jobs.admitted == []


def test_chosen_genres_reach_ranker_and_stay_on_final_recipe(store, monkeypatch):
    store.rec_surface.put_proposals("home_genre_request", {"epoch": 0, "include_genres": ["jazz"]}, 1000)
    home = service(store)
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


def test_pool_failure_and_polling_do_not_admit_jobs_or_record_offers(store, monkeypatch):
    home = service(store)
    parameters = {"include_genres": [], "avoid_genres": []}
    job = home.start_pool("fresh_pool", parameters, [])
    assert home.jobs.admitted == [("fresh_pool", parameters, [])]
    monkeypatch.setattr(store.discovery, "mark_offered", lambda *args: (_ for _ in ()).throw(AssertionError("not served")))
    assert home.pool(job, 1000, "/home/fresh-pool/1").pool["building"]
    job.done, job.error = True, "Provider unavailable"
    result = home.pool(job, 1000, "/home/fresh-pool/1")
    assert result.proto is None
    assert result.pool["message"] == "Provider unavailable"
    assert not result.pool["building"]
    assert len(home.jobs.admitted) == 1


def test_measurement_failure_keeps_offers_available(store, monkeypatch):
    home = service(store)
    monkeypatch.setattr(home_cards, "_one_card", lambda store, lane, now, **kw: proto() if lane == "wheelhouse" else None)

    def unavailable(*args, **kwargs):
        raise OSError("measurement unavailable")

    monkeypatch.setattr(home_cards, "_record_served_cards", unavailable)
    assert home.cards(home_cards.CardsRequest(1000)).protos[0]["tracks"][0]["key"] == "a|b"


def test_catalog_excludes_only_displayed_wheelhouse_tracks(store, monkeypatch):
    shared = []
    for i in range(60):
        title, artist = f"Song {i}", f"Artist {i}"
        store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        shared.append(surfaces.ForYouItem(title, artist, "", f"v{i}", None, 0, "",
                                         key=f"song {i}|artist {i}"))
    monkeypatch.setattr(surfaces, "for_you", lambda *a, **kw: shared)

    result = service(store).cards(home_cards.CardsRequest(1000))
    by_lane = {p["lane"]: p["tracks"] for p in result.protos}

    wheelhouse = {t.key for t in by_lane["wheelhouse"]}
    catalog = {t.key for t in by_lane["explore"]}
    assert len(wheelhouse) == len(catalog) == 12
    assert wheelhouse.isdisjoint(catalog)
