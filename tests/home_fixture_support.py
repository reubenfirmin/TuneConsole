"""Small published Home menus for tests of consumers, not preparation."""
import pytest

from tests.conftest import seed_catalog_track
from tests.store_fixture_support import initialize_consumer
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cards, home_prepared


@pytest.fixture
def browser_store(tmp_path, _pristine_current_schema):
    """Exercise live serving and background writes through installed WAL readers."""
    database = Store(tmp_path / 'home.db')
    try:
        initialize_consumer(database, _pristine_current_schema)
        yield database
    finally:
        database.conn.close()


def returning_listener(store, *formats, now=1):
    """Retain completed setup and past listening, independently of today's menus."""
    usage = store.rec_proposals.choice_usage()
    episodes = usage.setdefault('episodes', {})
    for format in formats:
        if format == 'vibes':
            usage['vibe_episodes'] = max(1, usage.get('vibe_episodes', 0))
        else:
            episodes[format] = max(1, episodes.get(format, 0))
    store.rec_proposals.put_proposals('listening_choices', {'home_format': usage}, now)
    store.settings.set_setting('onboard_setup_complete', '1')


def prepared_client(store, now=1000):
    """Create the ordinary HTTP app after restoring genuinely prepared Home."""
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from yt_playlist.web.app import create_app

    assert home_prepared._valid(store.rec_proposals.get_proposals(home_prepared.SURFACE))
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    return TestClient(create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: now),
                      base_url='http://127.0.0.1')


def publish_menu(store, now=1000, *, prefix='prepared', count=4, tracks_per_card=4,
                 surface='home_prepared', modeled=False):
    """Publish real subjects through the cache writer without running ranking.

    Different prefixes own disjoint tracks. Preparation, readiness and ranking
    tests should continue to build their own real inputs.
    """
    protos = []
    for index, lane in enumerate(('comfort', 'wheelhouse', 'explore', 'temporal')[:count]):
        tracks = []
        for offset in range(tracks_per_card):
            number = index * tracks_per_card + offset
            title, video = f'{prefix.title()} {number}', f'{prefix}-{number}'
            key = f'{prefix} {number}|artist'
            seed_catalog_track(store, key, title, 'Artist', video_id=video, genre='house')
            tracks.append({'key': key, 'title': title, 'artist': 'Artist', 'video_id': video,
                           'album': '', 'thumbnail': None, 'genre': 'house'})
        proto = home_cards._proto(store, lane, f'A {lane} mix for you', tracks, now)
        proto['recipe'] = {'lane': lane}
        if modeled:
            proto['mode_id'] = None
        protos.append(proto)
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], home_cards._epoch(store, 'cards'), None)
    previous = store.rec_proposals.get_proposals('home_prepared') if surface == 'home_tuned' else None
    home_prepared.remember(store, previous, result, now)
    payload = store.rec_proposals.get_proposals('home_tuned')
    assert home_prepared._valid(payload)
    if surface != 'home_tuned':
        store.rec_proposals.put_proposals(surface, payload, now)
        store.rec_proposals.put_proposals('home_tuned', {}, now)
        store.rec_proposals.put_proposals('home_choice', {}, now)
    return payload
