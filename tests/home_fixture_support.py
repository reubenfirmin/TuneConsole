"""Small published Home menus for tests of consumers, not preparation."""
import logging

import pytest

from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import initialize_consumer
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import home_cache, home_cards, home_prepared


def service(store):
    """Real card service without importing HTTP tests or the web lifecycle."""
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


@pytest.fixture
def home_routes_only(monkeypatch):
    """Keep real Home HTTP handling without constructing unrelated routers."""
    install_home_routes(monkeypatch)


def install_home_routes(monkeypatch):
    """Use the same real Home routers in consumers and prepared HTTP inputs."""
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home

    monkeypatch.setattr(webapp, 'build_all',
                        lambda ctx: [app_update.build(ctx), audio_preview.build(ctx), home.build(ctx)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)


@pytest.fixture
def home_control_routes(monkeypatch, home_routes_only):
    """Construct the real card/control routes and their application lifecycle."""
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, home_board, home_discovery, home_tuning
    from yt_playlist.web.routes.home_views import HomeViews

    def build(ctx):
        views = HomeViews(ctx)
        return [app_update.build(ctx), home_board.build(ctx, views), home_tuning.build(ctx, views),
                home_discovery.build(ctx, views)]

    monkeypatch.setattr(webapp, 'build_all', build)


@pytest.fixture(scope='module')
def home_page_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('home-page-bytecode')))


@pytest.fixture
def home_pages_only(monkeypatch, home_page_bytecode):
    """Real Home and the pages used to check its shared layout and setup state."""
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, charts, collection, home, playlists, sync

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_preview, charts, collection, home, playlists, sync)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    # Cache source-checked compiled code, keeping each app's environment,
    # globals, database reads and rendered responses private. Cold compilation
    # remains charged to the first request that actually uses each template.
    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


@pytest.fixture
def without_metadata_providers(monkeypatch):
    """Home consumers use catalog reads, independently of metadata tag matching."""
    from yt_playlist.providers import genres

    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)


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


def prepared_client(store, now=1000, *, background_work=True, backend_options=None):
    """Create the ordinary HTTP app after restoring genuinely prepared Home."""
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from yt_playlist.web.app import create_app

    assert home_prepared._valid(home_cache.read(store, home_prepared.SURFACE))
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    return TestClient(create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: now,
                                 background_work=background_work),
                      base_url='http://127.0.0.1', backend_options=backend_options)


def publish_menu(store, now=1000, *, prefix='prepared', count=4, tracks_per_card=4,
                 surface='home_prepared', modeled=False, mode_ids=(), genre_options=(), genre='house',
                 labels=(), vibe=None):
    """Publish real subjects through the cache writer without running ranking.

    Different prefixes own disjoint tracks. Preparation, readiness and ranking
    tests should continue to build their own real inputs.
    """
    protos = seed_menu_cards(store, now, prefix=prefix, count=count, tracks_per_card=tracks_per_card,
                             modeled=modeled, mode_ids=mode_ids, genre=genre, labels=labels, vibe=vibe)
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, list(genre_options), [], home_cards._epoch(store, 'cards'), vibe)
    return publish_cards(store, result, now, surface=surface)


def seed_menu_cards(store, now=1000, *, prefix='prepared', count=4, tracks_per_card=4,
                    modeled=False, mode_ids=(), genre='house', labels=(), vibe=None):
    """Seed real card subjects below consumers, without publishing an unused menu."""
    protos = []
    with atomic_write(store):
        for index, lane in enumerate(('comfort', 'wheelhouse', 'explore', 'temporal')[:count]):
            tracks = []
            for offset in range(tracks_per_card):
                number = index * tracks_per_card + offset
                title, video = f'{prefix.title()} {number}', f'{prefix}-{number}'
                key = f'{prefix} {number}|artist'
                tracks.append({'key': key, 'title': title, 'artist': 'Artist', 'video_id': video,
                               'album': '', 'thumbnail': None, 'genre': genre})
            seed_publication_subjects(store, tracks)
            # Preserve the catalog's artist credits; metadata ingestion is not
            # part of a prepared-menu consumer fixture.
            from yt_playlist.core import artists
            for track in tracks:
                recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?',
                                               (track['key'],)).fetchone()[0]
                artists.observe(store.conn, recording, track['artist'])
            label = labels[index] if index < len(labels) else f'A {lane} mix for you'
            proto = home_cards._proto(store, lane, label, tracks, now, vibe=vibe)
            proto['recipe'] = {'lane': lane}
            if vibe is not None:
                proto['recipe']['vibe'] = vibe
            if index < len(mode_ids):
                proto['mode_id'] = mode_ids[index]
                proto['recipe'].update(model='mode', mode_id=mode_ids[index])
            elif modeled:
                proto['mode_id'] = None
            protos.append(proto)
    return protos


def publish_cards(store, result, now, *, surface='home_prepared'):
    """Persist completed cards below consumers without running the whole worker."""
    previous = home_cache.read(store, 'home_prepared') if surface == 'home_tuned' else None
    home_prepared.remember(store, previous, result, now)
    payload = home_cache.read(store, 'home_tuned')
    assert home_prepared._valid(payload)
    if surface != 'home_tuned':
        home_cache.put(store, surface, payload, now)
        home_cache.put(store, 'home_tuned', {}, now)
        home_cache.put(store, 'home_choice', {}, now)
    return payload
