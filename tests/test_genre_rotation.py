"""Real genre selection and HTTP consumers of independently prepared menus."""
import json

import pytest

from tests.home_fixture_support import home_routes_only, install_home_routes, returning_listener
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema
from tests.test_home_other_genres import (
    _client, _publish_menu, genre_bundles, genre_http_inputs, genre_route_context,
    genre_routes, install_genre_http_inputs, offered, template_bytecode,
)
from yt_playlist.rec import home_cache, home_cards as home, home_prepared, mode_surfaces

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'genre_http_inputs')

GENRES = ('Jazz', 'Techno', 'Trance', 'Rock', 'Ambient', 'Classical')


@pytest.fixture(scope='module')
def rotation_schema():
    entries = tuple((label.lower(), label) for label in GENRES)
    with bounded_consumer_schema(entries) as schema:
        yield schema


def test_rotation_schema_has_real_empty_owners_and_six_genre_families(rotation_schema):
    assert rotation_schema.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 0
    assert {row[0] for row in rotation_schema.execute('SELECT key FROM genre_family')} == {
        label.lower() for label in GENRES}
    assert rotation_schema.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert not rotation_schema.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def rotation_candidates(rotation_schema):
    def seed(source):
        returning_listener(source, 'mixes', now=1000)
        bundles = genre_bundles(source, genres=list(GENRES), tracks_per_lane=4)
        bundles['all'] = {}
        for mode in range(6):
            # Genre selection needs four distinct recordings per theme; lanes
            # can frame the same recordings without separate metadata ingestion.
            tracks = bundles[str(mode)]['wheelhouse']
            for track in tracks:
                track['video_id'] = 'rotation-' + track['key']
            for lane in mode_surfaces.CARD_SURFACES:
                items = [{**track, 'lane': lane} for track in tracks]
                bundles[str(mode)][lane] = items
                bundles['all'].setdefault(lane, []).extend(items)
        seed_publication_subjects(source, bundles['all']['wheelhouse'])
        publish_bundles(source, bundles, 1.0)
        return bundles

    with prepared_template(rotation_schema, seed) as template:
        yield template


def test_rotation_inputs_keep_six_real_modes_and_every_lane(rotation_candidates):
    source = rotation_candidates.source
    assert source.conn.execute('SELECT COUNT(*) FROM library_memberships').fetchone()[0] == 24
    assert len(source.modes.list_modes()) == 6
    assert source.recommendations.head('home') is not None
    selections = [(str(mode), lane, -1) for mode in range(6) for lane in mode_surfaces.CARD_SURFACES]
    # Capture one actual read view and use the serving owner's bounded batch,
    # instead of admitting 24 independent reader snapshots to inspect one input.
    with source.read_view() as reader:
        for offset in range(0, len(selections), 12):
            batch = selections[offset:offset + 12]
            pages = reader.recommendations.candidate_pages('home', batch, limit=5)
            for (mode, _lane, _after), page in zip(batch, pages, strict=True):
                tracks = page['tracks']
                assert len({track['key'] for track in tracks}) == 4
                assert {track['genre'] for track in tracks} == {GENRES[int(mode)]}
                assert all(track['video_id'] for track in tracks)
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def rotation_menu(rotation_candidates):
    with rotation_candidates.open(':memory:') as source:
        _publish_menu(source, 1000)
        yield capture_template(source, None)


def test_rotation_base_menu_prepares_four_distinct_themes(rotation_menu):
    payload = home_cache.read(rotation_menu.source, home_prepared.SURFACE)
    assert home_prepared._valid(payload)
    protos = payload['menus'][0]['protos']
    assert len(protos) >= 3
    assert len({track['genre'] for proto in protos for track in proto['tracks']}) == 4
    assert all(len(proto['tracks']) >= 4 for proto in protos)
    assert {option['family'] for option in payload['genre_options']} == {g.lower() for g in GENRES}


def test_rotation_base_menu_renders_four_actual_themes(rotation_menu):
    with rotation_menu.open(':memory:') as store:
        response = _client(store).get('/home/cards')
        assert response.status_code == 200
        assert len(offered(response)) == 4
        assert 'genre_request' not in response.text


@pytest.fixture(scope='module', params=['other', 'chosen'])
def requested_menu(request, rotation_menu, template_bytecode):
    # Produce the requested state once through the real HTTP route. Independent
    # consumers restore its actual SQL/files rather than repeating the producer.
    with rotation_menu.open(':memory:') as source:
        current = {track['genre'].lower() for proto in
                   home_cache.read(source, home_prepared.SURFACE)['menus'][0]['protos']
                   for track in proto['tracks']}
        before = (source.rec_model.get_weights(), source.rec_model.get_leans(),
                  source.recommendations.head('home'))
        with pytest.MonkeyPatch.context() as patch:
            install_home_routes(patch)
            install_genre_http_inputs(patch)
            with genre_route_context(patch, template_bytecode):
                client = _client(source)
                if request.param == 'other':
                    response = client.post('/home/other-genres', data={'genres': json.dumps(sorted(current))})
                else:
                    response = client.post('/home/mix-genres', data={'genres': ['jazz', 'techno']})
        assert response.status_code == 200
        result = {'kind': request.param, 'current': current, 'selected': offered(response),
                  'text': response.text, 'before': before}
        yield capture_template(source, result)


def test_genre_request_changes_themes_without_changing_taste_or_pools(requested_menu):
    result = requested_menu.result
    source = requested_menu.source
    assert (source.rec_model.get_weights(), source.rec_model.get_leans(),
            source.recommendations.head('home')) == result['before']
    assert home._epoch(source, 'cards') == 1
    saved = source.rec_proposals.get_proposals('home_genre_request')
    assert saved['epoch'] == 1
    assert 'genre_request' in result['text']
    if result['kind'] == 'other':
        assert len(result['selected']) == 2
        assert not result['selected'] & result['current']
        assert 'Different genres for this set' in result['text']
        assert set(saved['avoid_genres']) == result['current']
    else:
        assert result['selected'] == {'jazz', 'techno'}
        assert 'Your chosen genres' in result['text']
        assert '"kind": "chosen_genres"' in result['text']
        assert saved['include_genres'] == ['jazz', 'techno']


@pytest.mark.parametrize('operation', ['reread', 'breadth', 'refresh'])
def test_genre_request_survives_preview_and_expires_on_refresh(requested_menu, operation):
    with requested_menu.open(':memory:') as store:
        client = _client(store)
        if operation == 'reread':
            response = client.get('/home/cards')
        elif operation == 'breadth':
            response = client.post('/home/breadth', data={'breadth_bias': '0.5'})
        else:
            response = client.post('/home/refresh-cards')
        assert response.status_code == 200
        if operation == 'refresh':
            assert len(offered(response)) == 4
            assert 'genre_request' not in response.text
        else:
            assert offered(response) == requested_menu.result['selected']
            assert store.rec_proposals.get_proposals('home_genre_request')['epoch'] == 1
            assert home._epoch(store, 'cards') == 1
        before = requested_menu.result['before']
        assert (store.rec_model.get_weights(), store.rec_model.get_leans(),
                store.recommendations.head('home')) == before
