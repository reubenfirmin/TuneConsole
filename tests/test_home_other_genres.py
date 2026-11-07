import html
import json
import logging
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_bundle_subjects
from yt_playlist.rec import home_cache, home_prepared, home_themes, mode_surfaces
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key
from yt_playlist.rec import home_cards as home

pytestmark = pytest.mark.usefixtures('unlocked_home_controls')


@pytest.fixture(scope='module')
def genre_candidates_template(_pristine_current_schema):
    # Eight candidates still require general-pool backfill when a mode is
    # trimmed to four below. Extra copies do not add a filtering scenario.
    with prepared_template(_pristine_current_schema,
                           lambda source: seed_genres(source, tracks_per_lane=8)) as template:
        yield template


@pytest.fixture(scope='module')
def genre_controls_template(_pristine_current_schema):
    # HTTP controls need four playable tracks per lane. Backfill checks retain
    # the larger candidate template above and manipulate its real pools.
    def seed(source):
        bundles = genre_bundles(source, tracks_per_lane=4)
        # Genre steering needs four distinct recordings per theme, rather than
        # a disjoint catalog for every way of framing that same theme. Keep
        # all real mode/lane pools while sharing their input recordings.
        bundles['all'] = {}
        for mode in range(6):
            tracks = bundles[str(mode)]['wheelhouse']
            for lane in mode_surfaces.CARD_SURFACES:
                items = [{**track, 'lane': lane} for track in tracks]
                bundles[str(mode)][lane] = items
                bundles['all'].setdefault(lane, []).extend(items)
        seed_bundle_subjects(source, bundles)
        publish_bundles(source, bundles, 1.0)
        _publish_menu(source, 1000.0)
        return bundles

    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


@pytest.fixture
def genre_controls(store, genre_controls_template):
    return genre_controls_template.restore(store)


@pytest.fixture(scope='module')
def template_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('genre-template-bytecode')))


@pytest.fixture(autouse=True)
def genre_routes(monkeypatch, home_routes_only, template_bytecode):
    from yt_playlist.providers import genres as genre_provider
    from yt_playlist.web import app as webapp

    # Diagnostic history rereads every displayed track, independently of genre
    # steering. Its real payload/metadata contracts live in test_home_audit.
    monkeypatch.setattr(home.HomeCards, 'audit', lambda *args, **kwargs: None)
    # The metadata provider's global tag matcher is unrelated to Home's real
    # catalog resolution. Avoid rebuilding that separate subsystem per app.
    monkeypatch.setattr(genre_provider, 'configure', lambda *args, **kwargs: None)
    templates = webapp.Jinja2Templates

    def cached_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = template_bytecode
        return result

    # Share compiled source only; each app keeps its own environment, globals
    # and request context. The real templates still render every response.
    monkeypatch.setattr(webapp, 'Jinja2Templates', cached_templates)


def _publish_menu(store, now):
    # Publish the real genre selection once. Worker snapshot coordination,
    # discovery summaries and alternative-menu enumeration belong to
    # test_home_prepared, rather than every HTTP genre-control scenario.
    with store.read_view() as reader:
        result = home.HomeCards(reader, home_prepared._NoJobs(), logging.getLogger(__name__)).cards(
            home.CardsRequest(now, record=False))
        tuning = home_prepared.tuning_key(reader, now)
    home_prepared.remember(store, None, result, now, tuning=tuning)
    payload = home_cache.read(store, 'home_tuned')
    assert home_prepared._valid(payload)
    home_cache.put(store, home_prepared.SURFACE, payload, now)
    home_cache.put(store, 'home_tuned', {}, now)
    home_cache.put(store, 'home_choice', {}, now)


def _client(store, now=1000.0, *, prepare=True):
    from yt_playlist.web.app import create_app

    identity = store.accounts.upsert_account('main', 'cred', None, True)
    if prepare and not home_prepared._valid(home_cache.read(store, home_prepared.SURFACE)):
        _publish_menu(store, now)
    return TestClient(create_app(store, lambda: {identity: FakeClient()},
                                now_fn=lambda: now, background_work=False),
                      base_url='http://127.0.0.1')


def seed_genres(store, *, genres=None, tracks_per_lane=20, fresh=True):
    bundles = genre_bundles(store, genres=genres, tracks_per_lane=tracks_per_lane, fresh=fresh)
    seed_bundle_subjects(store, bundles)
    publish_bundles(store, bundles, 1.0)
    return bundles


def genre_bundles(store, *, genres=None, tracks_per_lane=20, fresh=True):
    """Create mode identities and complete candidate rows before subject ingestion."""
    genres = genres or ["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]
    store.modes.replace_modes([
        {"mode_id": mid, "label": genre, "families": [[genre_map.family(genre), 80]],
         "centroid": np.eye(len(genres), dtype=np.float32)[mid], "size": 80, "rep_keys": []}
        for mid, genre in enumerate(genres)], retired_ids=[], now=1.0)
    bundles = {"all": {}, "_meta": {"comfort_pool": 100, "year_cuts": None}}
    for mid, genre in enumerate(genres):
        bundles[str(mid)] = {}
        for lane in mode_surfaces.CARD_SURFACES:
            if lane == 'fresh' and not fresh:
                continue
            items = [{"key": f"{lane}-{mid}-{i}|artist{i}", "video_id": "v", "title": f"Song {mid} {i}",
                      "artist": f"Artist {i}", "album": "", "thumbnail": None, "genre": genre,
                      "plays": 0, "reason": "", "lane": lane} for i in range(tracks_per_lane)]
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    return bundles


def offered(response):
    return {g for value in re.findall(r"data-genres='([^']*)'", response.text)
            for g in json.loads(html.unescape(value))}


def test_other_genres_changes_actual_themes_without_changing_taste(store, genre_controls):
    client = _client(store)
    before = client.get("/home/cards")
    current = offered(before)
    assert len(current) == 4
    generation = store.recommendations.head('home')
    weights, leans = store.rec_model.get_weights(), store.rec_model.get_leans()
    response = client.post("/home/other-genres", data={"genres": json.dumps(sorted(current))})
    assert response.status_code == 200
    assert len(offered(response)) == 2
    assert not offered(response) & current
    assert "Different genres for this set" in response.text
    assert "genre_request" in response.text  # saved recipe and diagnostic audit carry the request
    assert store.rec_model.get_weights() == weights and store.rec_model.get_leans() == leans
    assert store.recommendations.head('home') == generation
    assert home._epoch(store, "cards") == 1

    # Re-fetching and steering preserve this menu's request. A normal refresh ends it.
    assert offered(client.get("/home/cards")) == offered(response)
    preview = client.post("/home/breadth", data={"breadth_bias": "0.5"})
    assert offered(preview) == offered(response)
    regular = client.post("/home/refresh-cards")
    assert len(offered(regular)) == 4
    assert "genre_request" not in regular.text


def test_no_alternative_genres_keeps_usable_mixes_and_explains(store, genre_controls):
    response = _client(store).post("/home/other-genres", data={
        "genres": json.dumps(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"])})
    assert response.status_code == 200
    assert "No other genre themes are ready yet" in response.text
    assert len(offered(response)) == 4


def test_invalid_genre_request_does_not_advance_rotation(store):
    client = _client(store, prepare=False)
    for value in ["broken", "null", '"jazz"', "[]", "[4]", '[""]', "{}"]:
        assert client.post("/home/other-genres", data={"genres": value}).status_code == 400
    assert home._epoch(store, "cards") == 0
    assert store.rec_proposals.get_proposals("home_genre_request") is None


def test_genre_filter_uses_families_and_artist_fallback(store, monkeypatch):
    monkeypatch.setattr(store.rec_query, "artist_genre_years_for", lambda names: {"A": {"genre": "Jazz"}})
    items = [{"key": "known", "genre": "Jazz"}, {"key": "inferred", "artist": "A"},
             {"key": "unknown"}, {"key": "new", "genre": "Techno"}]
    assert home_themes.matching_genres(store, items, avoid={genre_map.family("Jazz")}) == [items[-1]]


def test_other_genres_never_backfills_the_previous_genres(store, genre_candidates):
    bundles = genre_candidates
    for mid in range(1, 6):
        bundles[str(mid)] = {}
    # Thin new-genre buckets used to be topped up with arbitrary general-pool genres.
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] = bundles["0"][lane][:4]
    publish_bundles(store, bundles, 1.0)
    avoid = {genre_map.family(g) for g in ["Techno", "Trance", "Indie Rock", "Ambient", "Classical"]}
    cards = mode_surfaces.assemble_cards(store, 1000, 1, avoid_genres=avoid)
    assert cards
    assert all(len(card['tracks']) >= 4 for card in cards)
    assert all(t["genre"] == "Jazz" for c in cards for t in c["tracks"])


def test_other_genres_does_not_attribute_a_rethemed_card_to_the_old_mode(store, genre_candidates):
    bundles = genre_candidates
    # The jazz-led mode has enough techno in its bucket to survive a track-only filter.
    # Showing those leftovers under the jazz mode would teach the wrong theme on a pick.
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] += bundles["1"][lane]
    publish_bundles(store, bundles, 1.0)
    cards = mode_surfaces.assemble_cards(store, 1000, 1, avoid_genres={genre_map.family("Jazz")})
    assert cards and all(c["mode_id"] != 0 for c in cards)
    assert all(t["genre"] != "Jazz" for c in cards for t in c["tracks"])


def test_other_genres_uses_current_library_genres_before_rendering(store, genre_controls):
    bundles = genre_controls
    # The worker cached Techno; enrichment has since corrected these tracks to Jazz.
    # Leave other alternatives available so this must succeed without the usual-mix fallback.
    generation = store.recommendations.head('home')
    for item in {item['key']: item for items in bundles["1"].values() for item in items}.values():
        assert item['genre'] == 'Techno'
        entry = store.conn.execute(
            'SELECT e.id FROM catalog_entries e JOIN recordings r ON r.id=e.recording_id '
            'WHERE r.identity_key=?', (item['key'],)).fetchone()
        assert entry is not None
        store.tracks.set_track_genre(entry[0], 'Jazz')
    # Do not republish: the real mode and general pools must still carry the
    # stale Techno payload while selection resolves the corrected catalog.
    client = _client(store)

    response = client.post("/home/other-genres", data={
        "genres": json.dumps(["Jazz", "Trance", "Indie Rock", "Classical"])})

    assert response.status_code == 200
    assert "Different genres for this set" in response.text
    assert offered(response) == {"ambient"}
    assert "jazz" not in offered(client.get("/home/cards"))
    assert store.recommendations.head('home') == generation


def test_genre_filter_resolves_library_metadata_without_mutating_candidates(store):
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    items = [{"key": identity_key("Song", "Artist"), "genre": "Jazz"}]

    result = home_themes.matching_genres(store, items, avoid={genre_map.family("Jazz")})

    assert len(result) == 1 and result[0]["genre"] == "Techno"
    assert items[0]["genre"] == "Jazz"


def test_chosen_genres_constrain_tracks_and_expire_without_changing_taste(store, genre_controls):
    client = _client(store)
    weights, leans = store.rec_model.get_weights(), store.rec_model.get_leans()
    generation = store.recommendations.head('home')
    response = client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]})

    assert response.status_code == 200
    assert offered(response) == {"jazz", "techno"}
    assert "Your chosen genres" in response.text
    assert '"kind": "chosen_genres"' in response.text
    assert store.rec_model.get_weights() == weights and store.rec_model.get_leans() == leans
    assert store.recommendations.head('home') == generation
    assert offered(client.get("/home/cards")) == offered(response)
    assert offered(client.post("/home/breadth", data={"breadth_bias": "0.5"})) == offered(response)
    regular = client.post("/home/refresh-cards")
    assert len(offered(regular)) == 4
    assert '"kind": "chosen_genres"' not in regular.text


def test_chosen_genres_filter_backfill_and_keep_mode_attribution(store, genre_candidates):
    bundles = genre_candidates
    for lane in mode_surfaces.CARD_SURFACES:
        # A jazz-led mode contains techno; a techno-led mode needs general backfill.
        bundles["0"][lane] += bundles["1"][lane]
        bundles["1"][lane] = bundles["1"][lane][:4]
    publish_bundles(store, bundles, 1.0)
    cards = mode_surfaces.assemble_cards(store, 1000, 1, include_genres={"techno"})
    assert cards
    assert all(len(card['tracks']) >= 4 for card in cards)
    assert all(c["mode_id"] == 1 for c in cards)
    assert all(t["genre"] == "Techno" for c in cards for t in c["tracks"])


def test_chosen_genres_validate_before_advancing_the_menu(store, monkeypatch):
    from yt_playlist.web.routes.home_views import HomeViews

    # Validation consumes available options; candidate production and menu
    # preparation are independent lower-layer work. Keep real HTTP validation
    # and verify it cannot change the actual rotation or saved request.
    monkeypatch.setattr(HomeViews, 'prepared', lambda *args: {'genre_options': [
        {'family': 'jazz', 'label': 'jazz'}, {'family': 'techno', 'label': 'techno'}]})
    client = _client(store, prepare=False)
    for genres in [[], [""], ["not-available"], ["jazz", "not-available"], ["jazz"] * 101]:
        assert client.post("/home/mix-genres", data={"genres": genres}).status_code == 400
    assert home._epoch(store, "cards") == 0
    assert store.rec_proposals.get_proposals("home_genre_request") is None


def test_chosen_genres_keep_a_fresh_skeleton_when_the_pool_cannot_fill_a_mix(store):
    # Four Techno tracks per lane keep the usual menu usable. The requested
    # Jazz pool remains genuinely underfilled without preparing four unused
    # genre families or duplicate candidate tracks.
    bundles = genre_bundles(store, genres=['Jazz', 'Techno'], tracks_per_lane=4)
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] = bundles["0"][lane][:1]
        bundles["all"][lane] = [t for t in bundles["all"][lane] if t["genre"] != "Jazz"]
    seed_bundle_subjects(store, bundles)
    publish_bundles(store, bundles, 1.0)
    client = _client(store)
    assert 'techno' in offered(client.get('/home/cards'))
    response = client.post("/home/mix-genres", data={"genres": ["jazz"]})
    assert response.status_code == 200
    assert 'id="gen-fresh"' in response.text and 'class="fresh-pool-art"' in response.text
    assert 'hx-post="/home/fresh-pool"' in response.text
    assert 'data-requested-genres=\'["jazz"]\'' in response.text
    assert "Here are your usual mixes" not in response.text
    assert not offered(response)


def test_available_genres_follow_current_metadata_and_artist_fallback(store):
    from tests.conftest import seed_catalog_track
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    seed_catalog_track(store, 'artist-genre', 'Genre evidence', 'A', video_id='artist-genre', genre='Jazz')
    seed_catalog_track(store, 'inferred', 'Inferred', 'A', video_id='inferred')
    seed_catalog_track(store, 'unknown', 'Unknown', 'B', video_id='unknown')
    items = [{"key": identity_key("Song", "Artist"), "genre": "Ambient"},
             {"key": "inferred", "artist": "A"}, {"key": "unknown"}]
    publish_bundles(store, {"all": {"wheelhouse": items}}, 1.0)
    assert home_themes.genre_options(store) == [
        {"family": "jazz", "label": "jazz"}, {"family": "techno", "label": "techno"}]
    assert [item["key"] for item in home_themes.matching_genres(store, items, include={"jazz"})] == ["inferred"]
    assert items[0]["genre"] == "Ambient"


def test_available_genres_prioritize_listening_over_alphabet_and_candidate_volume(store, genre_candidates):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    for genre, count in {"Trance": 100, "Techno": 60, "Jazz": 20, "Ambient": 1}.items():
        tid = store.tracks.upsert_track(genre, genre, "Artist", None, None)
        store.tracks.set_track_genre(tid, genre)
        for day in range(count):
            store.history.record_history_plays(iid, day * 86400.0, [identity_key(genre, "Artist")], source_timezone="UTC")
    options = home_themes.genre_options(store)
    assert [option["label"] for option in options[:3]] == ["trance", "techno", "jazz"]
