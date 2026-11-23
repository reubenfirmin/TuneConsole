import importlib.util
import inspect
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from contextlib import ExitStack

import pytest
from tests.audio_model_support import disable_models
from tests.genre_catalog_support import use_shipped_catalog
from tests.store_fixture_support import consumer_schema, initialize_consumer, native_sql_calls
from yt_playlist.core.store import Store


@pytest.fixture
def gpg_home():
    # Unix agent sockets must fit sun_path even when pytest's fixture root is
    # beneath a long checkout/report path. Own only this unique agent and home.
    with TemporaryDirectory(prefix='tc-gpg-', dir='/tmp') as directory:
        try:
            yield Path(directory)
        finally:
            if shutil.which('gpgconf'):
                subprocess.run(['gpgconf', '--homedir', directory, '--kill', 'gpg-agent'],
                               capture_output=True, timeout=10)


# A sync Playwright driver owns a running asyncio loop until its fixture ends.
# Apply the same lifetime to every browser test, including packaged tests outside
# tests/browser/, so mixed runs can return to asyncio.run() in later modules.
def browser_fixture_scope(*, fixture_name, config):
    return "session" if config.getoption("markexpr") == "browser" else "module"


if importlib.util.find_spec("pytest_playwright") is not None:
    from pytest_playwright import pytest_playwright as browser_fixtures

    playwright = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.playwright))
    browser_type = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser_type))
    launch_browser = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.launch_browser))
    browser = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser))
    browser_context_args = pytest.fixture(scope=browser_fixture_scope)(inspect.unwrap(browser_fixtures.browser_context_args))


@pytest.fixture(autouse=True)
def isolated_app_home(tmp_path, monkeypatch):
    """Backups, action locks and settings must never touch the user's app data."""
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path / "app-home"))
    # A developer's opt-in .env.local must not enable automatic audio in other tests.
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    disable_models(monkeypatch)
    # A developer's paid provider key must never turn an ordinary test into a billable call.
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync(monkeypatch):
    """Tests drive sync/update checks explicitly, without periodic jobs leaking into later tests."""
    from yt_playlist.web import app as webapp

    monkeypatch.setattr(webapp, "_background_sync_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, "BACKGROUND_WORK", False)


@pytest.fixture(scope="session")
def _pristine_current_schema():
    """Read-only shipped SQL below tests of application consumers.

    Fresh-bootstrap and historical-upgrade tests construct their own Stores.
    Every consumer still owns a new connection, file namespace and revision
    guards. Startup admission belongs to tests that call it themselves.
    """
    with consumer_schema() as template:
        yield template


@pytest.fixture
def store(_pristine_current_schema, request):
    s = Store(":memory:")
    try:
        initialize_consumer(s, _pristine_current_schema)
        if 'genre_pools' in request.fixturenames:
            # The DJ account and session fixtures must be added after cloning:
            # restoring from genre_pools itself would erase their SQL state.
            s._fixture_genre_pools = request.getfixturevalue('radio_genre_template').restore(s)
        if 'poc' in request.fixturenames:
            # Restore before downstream fixtures add accounts or custom seeds.
            if 'genre_pools' in request.fixturenames:
                from tests.radio_session_support import seed_pool

                s._fixture_radio_pool = seed_pool(s)
            else:
                s._fixture_radio_pool = request.getfixturevalue('radio_catalog_template').restore(s)
        if 'radio_ui' in request.fixturenames:
            request.getfixturevalue('radio_browser_template').restore(s)
        yield s
    finally:
        s.conn.close()


@pytest.fixture(scope='session')
def stored_fresh_template(_pristine_current_schema, tmp_path_factory):
    from tests.prepared_fixture_support import prepared_template
    from tests.rec.test_stored_fresh import seed_stored_fresh
    from tests.store_fixture_support import use_native_sql_calls

    def seed(source):
        with pytest.MonkeyPatch.context() as patch:
            use_native_sql_calls(patch)
            return seed_stored_fresh(source)

    database = tmp_path_factory.mktemp('stored-fresh') / 'source.db'
    with prepared_template(_pristine_current_schema, seed, database=database) as template:
        yield template


@pytest.fixture(scope='session')
def stored_radio_template(stored_fresh_template, tmp_path_factory):
    from tests.prepared_fixture_support import capture_template
    from tests.rec.test_stored_radio import seed_stored_radio

    database = tmp_path_factory.mktemp('stored-radio') / 'source.db'
    with stored_fresh_template.open(database) as source:
        yield capture_template(source, seed_stored_radio(source))


@pytest.fixture(scope='session')
def vibe_menu_template(_pristine_current_schema, tmp_path_factory):
    from tests.prepared_fixture_support import prepared_template
    from tests.store_fixture_support import reuse_reporting_reader
    from tests.test_growth_readiness import publish_vibe_menus

    # Register once here: importing a session fixture into several test modules
    # creates separate pytest owners and repeats the same profile preparation.
    def seed(source):
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(source, patch):
            return publish_vibe_menus(source)
    path = tmp_path_factory.mktemp('vibe-menu-inputs') / 'source.db'
    with prepared_template(_pristine_current_schema, seed, database=path) as template:
        yield template


@pytest.fixture
def vibe_menus(store, vibe_menu_template):
    return vibe_menu_template.restore(store)


@pytest.fixture(scope='session')
def recent_growth_vibes(tmp_path_factory):
    from tests.prepared_fixture_support import prepared_template
    from tests.store_fixture_support import reuse_reporting_reader
    from tests.test_growth_readiness import publish_vibe_menus

    def seed(source):
        source.accounts.upsert_account('main', None, True)
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(source, patch):
            publish_vibe_menus(source, genre_reserves=False, retain_payload=False)

    database = tmp_path_factory.mktemp('recent-growth-vibes') / 'source.db'
    with consumer_schema() as schema, prepared_template(schema, seed, database=database) as template:
        yield template


@pytest.fixture
def recent_growth_store(tmp_path, recent_growth_vibes):
    with recent_growth_vibes.open(tmp_path / 'growth.db') as store:
        yield store


@pytest.fixture(scope='session')
def full_journey_prefix(_pristine_current_schema, vibe_menu_template):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_growth_destinations import publish_journey_prefix

    # Keep actual catalog subjects, profiles, policies and menu files. Account
    # setup, destination publication and live readiness remain with each case.
    with prepared_template(_pristine_current_schema,
                           lambda source: publish_journey_prefix(source, vibe_template=vibe_menu_template)) as template:
        yield template


@pytest.fixture(scope="session")
def radio_browser_template(_pristine_current_schema):
    from tests.onboarding_support import publish_personal_mix
    from tests.prepared_fixture_support import prepared_template

    with prepared_template(_pristine_current_schema,
                           lambda source: publish_personal_mix(source, now=1)) as template:
        yield template


@pytest.fixture(scope="session")
def radio_catalog_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.radio_session_support import seed_pool

    with prepared_template(_pristine_current_schema, seed_pool) as template:
        yield template


@pytest.fixture(scope="session")
def radio_genre_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_radio_genres import seed_genre_pools

    # Keep all nine musical leaves, 108 recordings, metadata and typed publications.
    # Each consumer receives private SQL, retained files and detached rows.
    with prepared_template(_pristine_current_schema, seed_genre_pools) as template:
        yield template


@pytest.fixture(scope="session")
def measured_sounds_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_home_vibe import seed_sounds

    # Keep the complete default measured input. The distinct by-mode comparison
    # still builds its own audio pattern in the test that exercises it.
    with prepared_template(_pristine_current_schema, seed_sounds) as template:
        yield template


@pytest.fixture
def measured_sounds_store(store, measured_sounds_template):
    measured_sounds_template.restore(store)
    return store


@pytest.fixture(scope='session')
def measured_home_templates(_pristine_current_schema, measured_sounds_template):
    """Prepare each ordinary consumer prefix once, after the unchanged input boundary."""
    from tests.prepared_fixture_support import prepared_template

    with ExitStack() as resources:
        templates = {}

        def prepared(kind):
            assert kind in {'backend', 'browser'}
            if kind not in templates:
                def build(source):
                    measured_sounds_template.restore(source)
                    if kind == 'backend':
                        from tests.test_home_tuning import prepare_tuning_home
                        prepare_tuning_home(source, 1000)
                    else:
                        from tests.measured_home_support import prepare
                        identity = source.accounts.upsert_account('main', None, True)
                        # Match live_app's pre-preparation state, including its
                        # existing playlist. live_app later upserts these same
                        # identities while creating the private application.
                        source.playlists.upsert_playlist(identity, 'PLZ', 'Old Mix', 3, 'h', 0.0)
                        prepare(source)

                templates[kind] = resources.enter_context(
                    prepared_template(_pristine_current_schema, build))
            return templates[kind]

        yield prepared


@pytest.fixture
def measured_home_store(store, measured_home_templates):
    measured_home_templates('backend').restore(store)
    return store


@pytest.fixture
def measured_browser_home_store(store, measured_home_templates):
    measured_home_templates('browser').restore(store)
    return store


@pytest.fixture(scope="session")
def home_seed_templates(_pristine_current_schema):
    from tests.home_prepared_support import seed
    from tests.prepared_fixture_support import prepared_template
    from tests.recommendation_support import publish_bundles

    # Keep four- and eight-track consumers distinct, including the genuinely
    # unpublished cold-start input. No source has already prepared Home menus.
    with ExitStack() as resources:
        templates = {}

        def inputs(tracks_per_lane=8, *, publish=True):
            key = (tracks_per_lane, publish)
            if key not in templates:
                def build(source):
                    if not publish:
                        return seed(source, tracks_per_lane=tracks_per_lane, publish=False)
                    bundles = inputs(tracks_per_lane, publish=False).restore(source)
                    publish_bundles(source, bundles, vibes=True)
                    return bundles

                templates[key] = resources.enter_context(
                    prepared_template(_pristine_current_schema, build))
            return templates[key]

        yield inputs


@pytest.fixture(scope="session")
def home_seed_template(home_seed_templates):
    return home_seed_templates(4)


@pytest.fixture
def home_seed_inputs(store, home_seed_templates):
    """Private eight-track inputs, before the consumer's own preparation."""
    return home_seed_templates().restore(store)


@pytest.fixture(scope="session")
def home_prepared_template(_pristine_current_schema, home_seed_template):
    from tests.prepared_fixture_support import prepared_template
    from yt_playlist.rec import home_prepared

    def build(source):
        home_seed_template.restore(source)
        result = home_prepared.prepare(source, 1000)
        assert result is not None
        return result

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture(scope='session')
def mode_cards_inputs_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_mode_cards_route import _seed_bundles

    # Both original modes and every twenty-track surface, before Home
    # preparation or exposure. Fresh-only consumers still publish their change.
    with prepared_template(_pristine_current_schema, _seed_bundles) as template:
        yield template


@pytest.fixture(scope='session')
def mode_cards_home_template(_pristine_current_schema, mode_cards_inputs_template):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_mode_cards_route import prepare_home
    from yt_playlist.rec import home_prepared

    def build(source):
        mode_cards_inputs_template.restore(source)
        # Match _client's account-before-preparation order, without creating
        # an app or recording any route's offer on the shared source.
        source.accounts.upsert_account('main', None, True)
        prepare_home(source)
        return source.rec_proposals.get_proposals(home_prepared.SURFACE)

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture(scope='session')
def genre_candidates_template(_pristine_current_schema, tmp_path_factory):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_home_other_genres import seed_genres

    # Keep the original six genres, four lanes and twenty songs per lane.
    # Give memory input private evidence before function-scoped homes exist.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('genre-candidates-home')))
        with prepared_template(_pristine_current_schema,
                               seed_genres) as template:
            yield template


@pytest.fixture
def genre_candidates(store, genre_candidates_template):
    return genre_candidates_template.restore(store)


@pytest.fixture(scope='session')
def fresh_genre_candidates_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_fresh_pool import _without_fresh

    with prepared_template(_pristine_current_schema, _without_fresh) as template:
        yield template


@pytest.fixture(scope='session')
def fresh_home_template(_pristine_current_schema, fresh_genre_candidates_template):
    from tests.home_fixture_support import returning_listener
    from tests.prepared_fixture_support import prepared_template
    from tests.test_mode_cards_route import prepare_home
    from yt_playlist.rec import home_prepared

    def build(source):
        fresh_genre_candidates_template.restore(source)
        returning_listener(source, 'mixes', now=1000)
        source.accounts.upsert_account('main', None, True)
        prepare_home(source)
        return source.rec_proposals.get_proposals(home_prepared.SURFACE)

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture(scope='session')
def adaptive_library_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_group_home import library

    # Real group discovery, bundles and serving, with no prepared Home menus.
    # In particular, the history-retirement regression must accumulate its own.
    with prepared_template(_pristine_current_schema, library) as template:
        yield template



@pytest.fixture(scope="session")
def _pristine_installed_schema(_pristine_current_schema):
    """Only the supported numbered layout belongs in consumer matrices."""
    def template(version):
        assert version == 107, 'Pre-release schemas are reset rather than converted'
        return _pristine_current_schema
    return template


@pytest.fixture(scope='session')
def _admitted_installed_schema(tmp_path_factory, _pristine_installed_schema):
    """Admission is setup for publication consumers, owned separately in startup tests."""
    from tests.prepared_fixture_support import prepared_template

    root = tmp_path_factory.mktemp('admitted-installed-schemas')
    templates = {}
    with ExitStack() as resources:
        def template(version):
            if version not in templates:
                admitted = resources.enter_context(prepared_template(
                    _pristine_installed_schema(version), lambda source: None,
                    database=root / f'{version}.db', run_migrations=True))
                assert admitted.source.conn.execute(
                    'SELECT max(version) FROM schema_migrations').fetchone()[0] == version
                # PreparedTemplate owns the admitted source's revision tracker
                # and installs its TEMP guards when restoring each consumer.
                templates[version] = admitted
            return templates[version]

        yield template


@pytest.fixture(scope='session')
def _bounded_installed_schema(tmp_path_factory):
    """Build empty publication inputs below admission, with real TEMP guards."""
    from tests.prepared_fixture_support import prepared_template
    from tests.store_fixture_support import bounded_consumer_schema
    from yt_playlist.core.migrations.runner import RELEASE_VERSION

    root = tmp_path_factory.mktemp('bounded-installed-schemas')
    templates = {}
    with ExitStack() as resources:
        def template(version):
            assert version == RELEASE_VERSION, 'Pre-release schemas are reset rather than converted'
            if version not in templates:
                with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as schema:
                    templates[version] = resources.enter_context(prepared_template(
                        schema, lambda source: None, database=root / f'{version}.db'))
                assert templates[version].source.conn.execute(
                    'SELECT max(version) FROM schema_migrations').fetchone()[0] == version
            return templates[version]

        yield template


@pytest.fixture(scope='session')
def compact_group_template(tmp_path_factory, _pristine_current_schema):
    from tests.repos.test_compact_group_publication import compacted_groups

    root = tmp_path_factory.mktemp('compact-group-template')
    with compacted_groups(_pristine_current_schema, root / 'source.db') as template:
        yield template


@pytest.fixture
def consumer_store(_pristine_current_schema, request):
    """Create owned empty consumers; populated reopens still use real admission."""
    def create(path=":memory:", *, snapshot_guards=True):
        started = time.perf_counter()
        target = Store(path)
        request.addfinalizer(target.conn.close)
        try:
            # Disposable consumers require real commits and restart visibility,
            # without host power-loss durability for their private input files.
            target.conn.execute('PRAGMA synchronous=OFF')
            assert not target.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone(), \
                'Consumer templates are only for a new empty Store; populated reopens need init_schema()'
            initialize_consumer(target, _pristine_current_schema, snapshot_guards=snapshot_guards)
        finally:
            request.node.user_properties.append(('consumer_initialization_seconds', time.perf_counter() - started))
        return target
    return create


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema):
    s = Store(tmp_path / "store.db")
    try:
        initialize_consumer(s, _pristine_current_schema)
        yield s
    finally:
        s.conn.close()


@pytest.fixture
def unlocked_home_controls(monkeypatch):
    """Isolate existing post-unlock feature tests from onboarding preparation.

    Growth readiness and journey tests deliberately do not use this fixture.
    """
    from yt_playlist.rec import account_growth
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *_args, **_kwargs: True)

    monkeypatch.setattr(account_growth, 'view', lambda *_args, **_kwargs: {
        'nodes': [], 'trails': [], 'achievements': [], 'health': {'total': 0, 'processed': 0},
        'vibes_ready': True, 'ready_areas': {'directions': True, 'discover': True, 'recent': True},
        'ready_formats': {'mixes': True, 'albums': False, 'radio': False},
        'home_choice_available': False})


@pytest.fixture
def canonical_genre_matcher(monkeypatch):
    """Provider matching tests use the catalog rather than a prior app's settings."""
    from yt_playlist.providers import genres

    monkeypatch.setattr(genres, '_active', None)
    monkeypatch.setattr(genres, '_active_keys', None)
    matcher = genres.GenreMatcher.from_values(genres.build_canon(genres.builtin_names()))
    monkeypatch.setattr(genres, 'snapshot', lambda store: matcher)


def only_provider(store, name):
    """Configure the enrichment waterfall to run exactly one provider (others disabled), lets a
    route-level test exercise the single-icon waterfall while isolating one provider's behavior."""
    from yt_playlist.providers import enrichment as E
    E.save_config(store, [{"name": p["name"], "enabled": p["name"] == name} for p in E.PROVIDERS])


def seed_catalog_track(store, key, title, artist, *, video_id=None, track_id=None, **metadata):
    """Seed explicit catalog subjects for tests whose opaque keys/IDs are part of the fixture."""
    from yt_playlist.core.track_sources import folded, save_to_library

    with store.conn:
        rid = store.conn.execute(
            'INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            (track_id,key,title,artist)).lastrowid
        ref = None if video_id else 'fixture:'+key
        if folded(store.conn):
            store.conn.execute('INSERT INTO track_sources(id,recording_id,video_id,source_ref,title,artist,orig_title,orig_artist) '
                               'VALUES (?,?,?,?,?,?,?,?)', (rid,rid,video_id,ref,title,artist,title,artist))
            save_to_library(store.conn, (rid,), None, origin='fixture')
        else:
            if video_id:
                store.conn.execute(
                    'INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?,?)',
                    (video_id,rid,title,artist,title,artist))
            store.conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id,source_ref) VALUES (?,?,?,?)',
                               (rid,rid,video_id,ref))
            if not video_id:
                store.conn.execute('INSERT INTO unresolved_items(entry_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?)',
                                   (rid,title,artist,title,artist))
            store.conn.execute("INSERT INTO library_memberships(entry_id,origin) VALUES (?,'fixture')", (rid,))
        store.conn.execute("INSERT INTO recording_aliases VALUES (?,?,'fixture')", (key,rid))
        if metadata:
            from yt_playlist.core import metadata as metadata_owner
            # The metadata owner validates each field. Resolving the complete
            # catalog view's columns again for every seeded track is redundant.
            for field,value in metadata.items():
                metadata_owner.select(store.conn,rid,field,value,'fixture')
        from yt_playlist.core import artists
        artists.observe(store.conn,rid,artist,browse_id=metadata.get('artist_browse_id'))
    return rid


class FakeClient:
    def __init__(self, playlists=None, tracks=None, history=None, search_results=None, catalog=None,
                 albums=None, song_durations=None, uploads=None, artists=None):
        self._playlists = playlists or []          # [{"playlistId","title","count"}]
        self._tracks = tracks or {}                # {playlistId: [track dict, ...]}
        self._history = history or []              # [track dict, ...]
        self._search_results = search_results or []  # list returned by search()
        self._catalog = dict(catalog or {})        # {video_id: track_dict}
        self._song_durations = dict(song_durations or {})  # {video_id: seconds} for get_song
        self._albums = albums or {}                # {browseId: album dict (get_album shape)}
        self._uploads = uploads or {}              # {browseId: album dict (upload shape)}
        self._artists = artists or {}              # {browseId: artist dict (get_artist shape)}
        for tlist in self._tracks.values():        # auto-seed: a client knows its own tracks
            for t in tlist:
                if t.get("videoId"):
                    self._catalog.setdefault(t["videoId"], t)
        self.created = []; self.added = []; self.removed = []; self.deleted = []; self.edited = []
        self.rated = []                            # [(videoId, rating), ...] from rate_song
    def get_library_playlists(self, limit=25):
        playlists = list(self._playlists)
        return playlists[:limit] if limit is not None else playlists
    def get_playlist(self, playlistId, limit=100):
        if playlistId in self.deleted:
            from ytmusicapi.exceptions import YTMusicServerError
            raise YTMusicServerError("Server returned HTTP 404: Not Found.")
        tracks = list(self._tracks.get(playlistId, []))
        return {"tracks": tracks[:limit] if limit is not None else tracks}
    def get_history(self): return list(self._history)
    def get_library_albums(self, limit=25): return []
    def get_library_songs(self, limit=25): return []
    def get_album(self, browseId): return self._albums.get(browseId, {})
    def get_library_upload_album(self, browseId): return self._uploads.get(browseId, {})
    def get_artist(self, browseId): return self._artists.get(browseId, {})
    def create_playlist(self, title, description, *, video_ids=None):
        pid = f"PL_NEW_{len(self.created)}"; self.created.append((pid, title, description))
        self._tracks[pid] = [self._catalog[vid] for vid in video_ids or () if vid in self._catalog]
        return pid
    def add_playlist_items(self, playlistId, videoIds, *, duplicates=False):
        self.added.append((playlistId, list(videoIds)))
        dst = self._tracks.setdefault(playlistId, [])
        results = []
        for vid in videoIds:
            t = self._catalog.get(vid)
            if t is not None:
                dst.append(t)
                if t.get("setVideoId"):
                    results.append({"videoId": vid, "setVideoId": t["setVideoId"]})
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": results}
    def remove_playlist_items(self, playlistId, videos):
        self.removed.append((playlistId, list(videos)))
        handles = {v["setVideoId"] for v in videos if v.get("setVideoId")}
        gone = {v.get("videoId") for v in videos if not v.get("setVideoId")}
        self._tracks[playlistId] = [t for t in self._tracks.get(playlistId, [])
                                   if t.get("setVideoId") not in handles and t.get("videoId") not in gone]
    def delete_playlist(self, playlistId): self.deleted.append(playlistId)
    def rate_song(self, videoId, rating): self.rated.append((videoId, rating))
    def search(self, query, filter="songs"): return list(self._search_results)
    def get_song(self, videoId):
        secs = self._song_durations.get(videoId)
        if secs is None:
            secs = (self._catalog.get(videoId) or {}).get("duration_seconds")
        details = {"videoId": videoId}
        if secs is not None:
            details["lengthSeconds"] = str(secs)   # real ytmusicapi returns it as a string
        return {"videoDetails": details}
    def edit_playlist(self, playlistId, **kw):
        self.edited.append((playlistId, kw))
        move = kw.get("moveItem")
        if move:
            moved, successor = move if isinstance(move, tuple) else (move, None)
            tracks = self._tracks.get(playlistId, [])
            item = next(t for t in tracks if t.get("setVideoId") == moved)
            tracks.remove(item)
            index = next((i for i, t in enumerate(tracks) if t.get("setVideoId") == successor), len(tracks))
            tracks.insert(index, item)
        return "STATUS_SUCCEEDED"

def _track(vid, title, artist, dur=200, album="Alb", set_video_id=None):
    t = {"videoId": vid, "title": title, "artists": [{"name": artist}],
         "album": {"name": album}, "duration_seconds": dur}
    if set_video_id is not None:
        t["setVideoId"] = set_video_id
    return t
