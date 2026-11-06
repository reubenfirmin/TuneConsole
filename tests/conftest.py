import importlib.util
import inspect
import sqlite3
import time
from contextlib import ExitStack, closing

import pytest
from tests.store_fixture_support import PristineSchema, initialize_consumer
from yt_playlist.core.store import Store


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
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
    # A developer's paid provider key must never turn an ordinary test into a billable call.
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync(monkeypatch):
    """Tests drive sync/update checks explicitly, without periodic jobs leaking into later tests."""
    from yt_playlist.web import app as webapp

    monkeypatch.setattr(webapp, "_background_sync_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(webapp, "BACKGROUND_WORK", False)


@pytest.fixture(scope="session")
def _pristine_current_schema(tmp_path_factory):
    """Read-only SQL template built through real bootstrap and admission.

    Fresh-bootstrap and historical-upgrade tests construct their own Stores.
    Every consumer still owns a new connection and file namespace. Admission
    runs here; consumer copies bind their repositories and revision guards.
    """
    template = sqlite3.connect(":memory:", factory=PristineSchema)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("YT_PLAYLIST_HOME", str(tmp_path_factory.mktemp("schema-template-home")))
            patch.setenv("YT_PLAYLIST_AUDIO_VIBES", "0")
            patch.setenv("YT_PLAYLIST_AUDIO_VIBE_MODEL", "")
            patch.delenv("DEEPSEEK_API_KEY", raising=False)
            from yt_playlist.core.db_revisions import Revisions
            from yt_playlist.core.migrations import migrate

            # This fixture owns SQL construction, not application request
            # admission. Use one ordinary SQLite connection for the real
            # migration runner and revision guards, avoiding a second Store,
            # duplicate backup and per-statement diagnostic wrappers. Consumer
            # fixtures still bind their own real repositories and guards.
            template.execute('PRAGMA foreign_keys=ON')
            migrate(template)
            template.capture_revisions(Revisions(template))
        template.execute("PRAGMA query_only=ON")
        yield template
    finally:
        template.close()


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
def full_journey_prefix(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_growth_destinations import publish_journey_prefix

    # Keep actual catalog subjects, profiles, policies and menu files. Account
    # setup, destination publication and live readiness remain with each case.
    with prepared_template(_pristine_current_schema, publish_journey_prefix) as template:
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

    # Keep all nine modes, 108 recordings, metadata and typed publications.
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
                        identity = source.accounts.upsert_account('main', 'cred', None, True)
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
        source.accounts.upsert_account('main', 'cred', None, True)
        prepare_home(source)
        return source.rec_proposals.get_proposals(home_prepared.SURFACE)

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture(scope='session')
def genre_candidates_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_home_other_genres import seed_genres

    # Keep all six genres, four lanes and twenty songs per lane. Consumers
    # change these candidates before Home preparation, so stop at publication.
    with prepared_template(_pristine_current_schema, seed_genres) as template:
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
        source.accounts.upsert_account('main', 'cred', None, True)
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
def production64_template():
    from tests.db_rebuild_support import build_production64

    template = sqlite3.connect(':memory:')
    try:
        build_production64(template)
        template.execute('PRAGMA query_only=ON')
        yield template
    finally:
        template.close()


@pytest.fixture(scope="session")
def development93_template(production64_template):
    import time
    from tests.db_rebuild_support import build_development93
    from tests.prepared_fixture_support import coordinates

    with closing(sqlite3.connect(':memory:')) as template:
        started = time.perf_counter()
        build_development93(template, production64_template)
        # This historical input has SQL only; never lose retained files by
        # silently treating a populated file-backed owner as a SQL template.
        assert coordinates(template) == []
        seconds = time.perf_counter() - started
        template.execute('PRAGMA query_only=ON')
        yield template, seconds


@pytest.fixture(scope="session")
def _pristine_installed_schema(tmp_path_factory):
    """Lazily build genuine historical SQL once per version and worker.

    Consumers still reopen and admit independent databases. Only empty layouts
    qualify: retained file coordinates must never escape into a SQL-only clone.
    Populated upgrades and migration fault injection keep their own builders.
    """
    from yt_playlist.core.migrations import registry, runner

    root = tmp_path_factory.mktemp("installed-schema-templates")
    templates = {}
    with ExitStack() as resources:
        def template(version):
            if version not in templates:
                directory = root / str(version)
                directory.mkdir()
                with pytest.MonkeyPatch.context() as patch:
                    patch.setenv("YT_PLAYLIST_HOME", str(directory / "app-home"))
                    # Empty SQL construction needs neither Store diagnostics
                    # nor a disk commit for every historical version. Consumers
                    # still own real disk/Store admission and populated upgrades.
                    initial = resources.enter_context(closing(sqlite3.connect(":memory:")))
                    initial.row_factory = sqlite3.Row
                    initial.execute('PRAGMA foreign_keys=ON')
                    previous = max((old for old in templates if old < version), default=None)
                    if previous is not None:
                        templates[previous].backup(initial)
                    runner.migrate(initial, registry=registry.MIGRATIONS[:version])
                    for table, in initial.execute(
                            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                        quoted = '"' + table.replace('"', '""') + '"'
                        for column in initial.execute(f'PRAGMA table_info({quoted})'):
                            if column[1] == 'filename' or column[1].endswith('_filename'):
                                field = '"' + column[1].replace('"', '""') + '"'
                                assert initial.execute(
                                    f'SELECT 1 FROM {quoted} WHERE {field} IS NOT NULL LIMIT 1'
                                ).fetchone() is None, f"Installed template retains files: {table}.{column[1]}"
                    initial.execute("PRAGMA query_only=ON")
                    templates[version] = initial
            return templates[version]

        yield template


@pytest.fixture(scope='session')
def legacy_owner_schema(tmp_path_factory):
    """Preserve the exact empty v92/v98 builders used by owner cutover tests."""
    from tests.core.test_collection_state_migration import installed_92
    from tests.core.test_album_occurrences_migration import installed_98
    from tests.prepared_fixture_support import coordinates

    builders, templates = {92: installed_92, 98: installed_98}, {}
    root = tmp_path_factory.mktemp('legacy-owner-schemas')
    with ExitStack() as resources:
        def get(version):
            if version not in templates:
                with pytest.MonkeyPatch.context() as patch:
                    patch.setenv('YT_PLAYLIST_HOME', str(root / str(version)))
                    initial = builders[version](root / f'{version}.db')
                    try:
                        assert coordinates(initial.conn) == []
                        snapshot = resources.enter_context(closing(sqlite3.connect(':memory:')))
                        initial.conn.backup(snapshot)
                        snapshot.execute('PRAGMA query_only=ON')
                        templates[version] = snapshot
                    finally:
                        initial.conn.close()
            return templates[version]

        yield get


@pytest.fixture
def legacy_owner_store(legacy_owner_schema, record_property):
    import time

    with ExitStack() as resources:
        def open_store(path, version=92):
            began = time.perf_counter()
            template = legacy_owner_schema(version)
            record_property('legacy_prefix_seconds', time.perf_counter() - began)
            store = Store(path)
            resources.callback(store.conn.close)
            began = time.perf_counter()
            template.backup(store.conn)
            store.conn.execute('PRAGMA journal_mode=WAL')
            store._bind_repositories()
            assert store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == version
            assert not store.conn.in_transaction
            record_property('legacy_clone_seconds', time.perf_counter() - began)
            return store

        yield open_store


@pytest.fixture(scope='session')
def staged_custody_templates(tmp_path_factory, _pristine_installed_schema):
    from tests.prepared_fixture_support import historical_template

    templates = {}
    with ExitStack() as resources:
        def get(version, owner, build, *, admit=True, logical_coordinates=()):
            builder = getattr(build, 'func', build)
            key = (version, owner, builder.__module__, builder.__qualname__, admit,
                   tuple(logical_coordinates))
            if key not in templates:
                root = tmp_path_factory.mktemp('staged-custody')

                def prepare(source):
                    if admit:
                        source.init_schema(defer_metadata=True)
                    # historical_template binds before admission. An already
                    # installed regular schema keeps its schema cookie, so
                    # admission may not rebind its predecessor read views.
                    source._bind_repositories()
                    return build(source)

                templates[key] = resources.enter_context(historical_template(
                    _pristine_installed_schema(version), prepare, database=root / 'source.db',
                    logical_coordinates=logical_coordinates))
            return templates[key]

        yield get


@pytest.fixture
def staged_custody(staged_custody_templates, record_property):
    import time
    from yt_playlist.core.db_revisions import Revisions

    def restore(installed, owner, build, **options):
        store, version = installed
        began = time.perf_counter()
        template = staged_custody_templates(version, owner, build, **options)
        record_property('staged_custody_builder_seconds', time.perf_counter() - began)
        began = time.perf_counter()
        result = template.restore(store)
        # SQL backup excludes TEMP. Rebind the real historical readers and
        # private revision guards after replacing this consumer's main schema.
        store._bind_repositories()
        if hasattr(store, '_snapshot_revisions'):
            Revisions.unbind(store.conn)
            store._snapshot_revisions = Revisions(store.conn)
        record_property('staged_custody_restore_seconds', time.perf_counter() - began)
        return result

    return restore


@pytest.fixture(scope='session')
def legacy_radio_template(tmp_path_factory, _pristine_installed_schema):
    from tests.core.test_legacy_radio_upgrade import seed_installed_radio
    from tests.prepared_fixture_support import historical_template

    root = tmp_path_factory.mktemp('legacy-radio-template')
    with historical_template(_pristine_installed_schema(93), seed_installed_radio,
                             database=root / 'source.db') as template:
        yield template


@pytest.fixture
def installed_radio(legacy_radio_template, request):
    import json
    from yt_playlist.core import radio_storage

    def restore(path, *, account_id=3, playlist='RADIO'):
        store = Store(path)
        request.addfinalizer(store.conn.close)
        try:
            state, raw = legacy_radio_template.restore(store)
            if (account_id, playlist) != (3, 'RADIO'):
                state.update(identity=account_id, playlist=playlist)
                raw = json.dumps(state)
                with store.conn:
                    store.conn.execute('UPDATE settings SET value=? WHERE key=?',
                                       (raw, radio_storage.SESSION_KEY))
            return store, state, raw
        except BaseException:
            store.conn.close()
            raise

    return restore


@pytest.fixture(scope='session')
def compact_group_template(tmp_path_factory, _pristine_installed_schema):
    from tests.repos.test_compact_group_publication import compacted_groups

    root = tmp_path_factory.mktemp('compact-group-template')
    with compacted_groups(_pristine_installed_schema(69), root / 'source.db') as template:
        yield template


@pytest.fixture
def consumer_store(_pristine_current_schema, request):
    """Create owned empty consumers; populated reopens still use real admission."""
    def create(path=":memory:"):
        started = time.perf_counter()
        target = Store(path)
        request.addfinalizer(target.conn.close)
        try:
            assert not target.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone(), \
                'Consumer templates are only for a new empty Store; populated reopens need init_schema()'
            initialize_consumer(target, _pristine_current_schema)
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
        'nodes': [], 'achievements': [], 'health': {'total': 0, 'processed': 0},
        'vibes_ready': True, 'ready_areas': {'directions': True, 'discover': True, 'recent': True},
        'ready_formats': {'mixes': True, 'albums': False, 'radio': False},
        'home_choice_available': False})


@pytest.fixture
def canonical_genre_matcher(monkeypatch):
    """Provider matching tests use the catalog rather than a prior app's settings."""
    from yt_playlist.providers import genres

    monkeypatch.setattr(genres, '_active', None)
    monkeypatch.setattr(genres, '_active_keys', None)


@pytest.fixture
def legacy_store(tmp_path):
    """A genuine pre-catalog database for upgrade tests, never a damaged current schema."""
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS
    s = Store(tmp_path / 'legacy.db')
    try:
        migrate(s.conn, registry=MIGRATIONS[:7])
        s._bind_repositories()
        yield s
    finally:
        s.conn.close()


def only_provider(store, name):
    """Configure the enrichment waterfall to run exactly one provider (others disabled), lets a
    route-level test exercise the single-icon waterfall while isolating one provider's behavior."""
    from yt_playlist.providers import enrichment as E
    E.save_config(store, [{"name": p["name"], "enabled": p["name"] == name} for p in E.PROVIDERS])


def seed_catalog_track(store, key, title, artist, *, video_id=None, track_id=None, **metadata):
    """Seed explicit catalog subjects for tests whose opaque keys/IDs are part of the fixture."""
    with store.conn:
        rid = store.conn.execute(
            'INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            (track_id,key,title,artist)).lastrowid
        if video_id:
            store.conn.execute(
                'INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?,?)',
                (video_id,rid,title,artist,title,artist))
        store.conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id,source_ref) VALUES (?,?,?,?)',
                           (rid,rid,video_id,None if video_id else 'fixture:'+key))
        if not video_id:
            store.conn.execute('INSERT INTO unresolved_items(entry_id,title,artist,orig_title,orig_artist) VALUES (?,?,?,?,?)',
                               (rid,title,artist,title,artist))
        store.conn.execute("INSERT INTO library_memberships VALUES (?,'fixture',NULL)", (rid,))
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
    def get_album(self, browseId): return self._albums.get(browseId, {})
    def get_library_upload_album(self, browseId): return self._uploads.get(browseId, {})
    def get_artist(self, browseId): return self._artists.get(browseId, {})
    def create_playlist(self, title, description):  # description required, matching real YTMusic API
        pid = f"PL_NEW_{len(self.created)}"; self.created.append((pid, title, description))
        self._tracks.setdefault(pid, []); return pid
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
