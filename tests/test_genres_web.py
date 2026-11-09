"""Contract tests for Enrichment's genre editor and /genres/add|remove|reset.

Fast TestClient-level assertions: routes consume form data and return the
_partials/genre_list.html fragment that htmx swaps into #genre-list; the empty-add
error path returns a 422 OOB toast. (Live behavior lives in the browser suite.)

Coverage moved here from the old JSON-based test_web.py::test_genres_whitelist_editor.
"""
from fastapi.testclient import TestClient
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.store_fixture_support import bounded_consumer_schema, consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.core.store import Store
from yt_playlist.util import genre_catalog

import yt_playlist.providers.genres as g
from yt_playlist.web.app import create_app


@pytest.fixture(autouse=True)
def genre_routes(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, enrich, genres, home

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_preview, enrich, genres, home)])


@pytest.fixture(scope='module')
def editor_vocabulary():
    records, labels = genre_catalog.catalog()
    keys = {'rock', 'jazz', 'hip hop', 'house', 'deep house', 'synth pop'}
    keys.update(records[key]['family'] for key in tuple(keys)
                if records[key]['family'] in records)
    selected = {key: records[key] for key in keys}
    aliases = {label: value for label, value in labels.items() if value[0] in keys}
    families = {key: value for key, value in genre_catalog.family_labels().items()
                if key in {row['family'] for row in selected.values()}}
    return selected, aliases, families


@pytest.fixture(scope='module')
def editor_schema(editor_vocabulary):
    records, _, families = editor_vocabulary
    with bounded_consumer_schema(tuple((key, row['name']) for key, row in records.items()),
                                 snapshot_guards=False) as schema:
        # Restore the shipped relationships: a subgenre must not become a
        # family just because the SQL fixture has a smaller vocabulary.
        schema.execute('PRAGMA query_only=OFF')
        with schema:
            schema.executemany('INSERT OR IGNORE INTO genre_family(key,name) VALUES (?,?)', families.items())
            schema.executemany('UPDATE genre_catalog SET family_key=?,family_source=? WHERE key=?',
                               [(row['family'], row['family_source'], key) for key, row in records.items()])
            schema.execute('DELETE FROM genre_family WHERE key NOT IN (SELECT family_key FROM genre_catalog)')
        schema.execute('PRAGMA query_only=ON')
        yield schema


@pytest.fixture
def store(request, tmp_path, monkeypatch):
    if request.node.name == 'test_full_editor_reads_taxonomy_without_per_genre_queries':
        # Keep the actual complete vocabulary in the batching/scaling owner.
        schema = request.getfixturevalue('full_editor_schema')
    else:
        schema = request.getfixturevalue('editor_schema')
        records, labels, families = request.getfixturevalue('editor_vocabulary')
        # Editor behavior keeps shipped aliases and family relations. Catalog
        # curation and the full-size render are independent owners.
        monkeypatch.setattr(genre_catalog, 'catalog', lambda: (records, labels))
        monkeypatch.setattr(genre_catalog, 'family_labels', lambda: families)
    database = Store(tmp_path / 'genre-editor.db')
    with closing(database.conn):
        initialize_consumer(database, schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture(scope='module')
def full_editor_schema():
    # Genre editing uses direct repository reads, without background snapshots.
    with consumer_schema(snapshot_guards=False) as schema:
        yield schema


def _client(store):
    store.genres.configure_catalog()
    # base_url is local so state-changing POSTs pass the cross-origin guard.
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")


@pytest.fixture
def _repository_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from yt_playlist.controllers.genres import Genres
    from yt_playlist.repos.genres import GenreRepo
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import genres

    repository = Mock(spec_set=GenreRepo)
    repository.transaction.side_effect = nullcontext
    repository.uses_keyed_preferences.return_value = True
    repository.label_matches.return_value = set()
    repository.genre_name.return_value = None
    repository.matcher_facts.return_value = None
    store = SimpleNamespace(genres=Genres(repository, None))
    ctx = SimpleNamespace(store=store,
        templates=Jinja2Templates(directory=Path(webapp.__file__).with_name('templates')))
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(genres.build(ctx))
    monkeypatch.setattr(g, '_active', g._active)
    monkeypatch.setattr(g, '_active_keys', g._active_keys)
    with TestClient(app) as client:
        yield repository, client, store


def test_page_renders_chip_list_zone(_repository_client):
    from yt_playlist.controllers.enrichment_reports import EnrichmentReports
    from yt_playlist.controllers.enrichment_state import EnrichmentState
    from yt_playlist.controllers.settings import Settings
    from yt_playlist.repos.enrichment_reports import EnrichmentReportsRepo
    from yt_playlist.repos.enrichment_state import EnrichmentStateRepo
    from yt_playlist.repos.settings import SettingsRepo
    from yt_playlist.web.icons import arrow_icon
    from yt_playlist.web.routes import enrich
    from yt_playlist.web.theme import mix_launch_tokens

    repository, c, store = _repository_client
    repository.get_genre_whitelist.return_value = ['Rock']
    repository.taxonomy_facts.return_value = {
        'families': {'rock': 'Rock'}, 'aliases': [], 'redirects': {},
        'genres': [('rock', 'Rock', 'rock')]}
    reports = Mock(spec_set=EnrichmentReportsRepo)
    reports.coverage_stats.return_value = dict.fromkeys(
        ('total', 'processed', 'genre', 'year', 'bpm', 'energy', 'danceability'), 0)
    reports.queue_stats.return_value = {'total': 0, 'first_pass': 0, 'refresh': 0}
    reports.activity_stats.return_value = {'latest': None, 'recent': 0}
    reports.processed_timestamps.return_value = []
    reports.outstanding_conflicts.return_value = 0
    state = Mock(spec_set=EnrichmentStateRepo)
    state.retry_status_inputs.return_value = None
    state.artwork_retry_status.return_value = {'count': 0, 'retry_at': None}
    settings = Mock(spec_set=SettingsRepo)
    settings.get_setting.return_value = '1'
    store.enrichment_reports = EnrichmentReports(reports, None)
    store.enrichment_state = EnrichmentState(state, None, None, None, None, None, None, None)
    store.settings = Settings(settings, None, None)
    ctx = c.app.state.ctx
    ctx.now_fn, ctx.enrich_worker = lambda: 1.0, None
    # Global shell helpers are outside this genre-editor rendering contract.
    ctx.templates.env.globals.update(
        asset_v='test', arrow_icon=arrow_icon, mix_launch_tokens=mix_launch_tokens,
        storage_maintenance_status=lambda: {'active': False, 'warning': False},
        initial_setup_pending=lambda: False, has_multiple_identities=lambda: False,
        audio_preview_available=lambda: False, auth_expired={})
    c.app.include_router(enrich.build(ctx))
    redirect = c.get("/genres", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/enrich?advanced=genres#recognized-genre-tags"
    response = c.get(redirect.headers["location"])
    assert response.status_code == 200
    body = response.text
    assert "<h1>Enrichment</h1>" in body
    assert "Recognized genre tags" in body
    assert "Rock" in body                       # a built-in chip is server-rendered
    assert 'id="genre-list"' in body            # the htmx swap zone is present
    assert "genresTab" not in body              # the Alpine factory is no longer referenced


def test_add_returns_fragment_with_genre(_repository_client):
    repository, c, store = _repository_client
    before, after = ['Rock'], ['Rock', 'Whack-a-doodle']
    repository.get_genre_whitelist.side_effect = [before, after, after, after]
    repository.taxonomy_facts.return_value = {
        'families': {'rock': 'Rock'}, 'aliases': [], 'redirects': {},
        'genres': [('rock', 'Rock', 'rock'), ('whack a doodle', 'Whack-a-doodle', None)]}
    n = len(store.genres.get_genre_whitelist())
    r = c.post("/genres/add", data={"name": "Whack-a-doodle"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()   # a fragment, not the whole page
    assert "Whack-a-doodle" in r.text
    assert f"{n + 1} genres" in r.text               # count reflects the addition
    # The real controller requests persistence and reloads the live matcher.
    repository.insert_custom.assert_called_once_with('whack a doodle', 'Whack-a-doodle')
    repository.save_preference.assert_called_once_with('whack a doodle', enabled=True, origin='user')
    assert "Whack-a-doodle" in store.genres.get_genre_whitelist()
    assert g.pick_genre(["seen live", "whack-a-doodle"]) == "Whack-a-doodle"


def test_remove_drops_genre(store):
    c = _client(store)
    c.post("/genres/add", data={"name": "Whack-a-doodle"})
    r = c.post("/genres/remove", data={"name": "Whack-a-doodle"})
    assert r.status_code == 200
    assert "Whack-a-doodle" not in r.text
    assert "Whack-a-doodle" not in store.genres.get_genre_whitelist()
    assert g.pick_genre(["whack-a-doodle"]) is None


def test_reset_restores_defaults(store):
    c = _client(store)
    defaults = store.genres.get_genre_whitelist()
    n = len(defaults)
    c.post("/genres/add", data={"name": "Whack-a-doodle"})
    assert len(store.genres.get_genre_whitelist()) == n + 1
    r = c.post("/genres/reset")
    assert r.status_code == 200
    assert "Rock" in r.text and "Whack-a-doodle" not in r.text
    assert len(store.genres.get_genre_whitelist()) == n
    assert store.genres.get_genre_whitelist() == defaults


def test_empty_add_returns_oob_toast(_repository_client):
    repository, c, _ = _repository_client
    r = c.post("/genres/add", data={"name": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "enter a genre name" in r.text
    assert repository.mock_calls == []  # Validation must not reach persistence.


def test_equivalent_add_is_idempotent_and_alias_removal_disables_identity(store):
    c = _client(store)
    before = store.genres.get_genre_whitelist()
    c.post("/genres/add", data={"name": "hip-hop"})
    c.post("/genres/add", data={"name": "rap"})
    assert store.genres.get_genre_whitelist() == before
    c.post("/genres/remove", data={"name": "hip-hop"})
    assert g.pick_genre(["rap", "hiphop", "Hip Hop"]) is None


def test_search_matches_aliases_and_family_labels_without_changing_enabled_list(store):
    c = _client(store)
    before = store.genres.get_genre_whitelist()
    alias = c.get("/genres/list", params={"q": "rap"}).text
    assert "Hip Hop" in alias and "Deep House" not in alias
    family = c.get("/genres/list", params={"q": "House"}).text
    assert "Deep House" in family and 'aria-label="Jazz"' not in family
    assert "<details open>" in family
    assert store.genres.get_genre_whitelist() == before


def test_query_survives_edits_and_escapes_input(_repository_client):
    repository, c, _ = _repository_client
    repository.get_genre_whitelist.return_value = ['Deep House', 'Whack-a-doodle']
    repository.taxonomy_facts.return_value = {
        'families': {'house': 'House'}, 'aliases': [], 'redirects': {},
        'genres': [('deep house', 'Deep House', 'house'), ('whack a doodle', 'Whack-a-doodle', None)]}
    r = c.post("/genres/add", data={"name": "Whack-a-doodle", "q": "Whack"})
    assert r.status_code == 200
    assert "Whack-a-doodle" in r.text and "Deep House" not in r.text
    repository.insert_custom.assert_called_once_with('whack a doodle', 'Whack-a-doodle')
    repository.save_preference.assert_called_once_with('whack a doodle', enabled=True, origin='user')
    r = c.get("/genres/list", params={"q": "<script>bad</script>"})
    assert "<script>bad</script>" not in r.text
    assert "&lt;script&gt;bad&lt;/script&gt;" in r.text


def test_current_taxonomy_controls_follow_rename_alias_family_and_merge(store, unlocked_home_controls):
    client = _client(store)
    store.genres.rename_genre('synth pop','Renamed style')
    store.genres.set_genre_alias('Searchable custom alias','synth pop')
    store.genres.set_genre_family('synth pop','jazz')
    body = client.get('/genres/list',params={'q':'Searchable custom alias'}).text
    assert 'Renamed style' in body and 'Jazz' in body
    assert 'aria-label="Remove Synth Pop"' not in body
    options = client.get('/home/genres').json()['options']
    assert {'name':'synth pop','label':'Renamed style','kind':'genre'} in options
    store.genres.add_genre('Retired custom style')
    store.genres.merge_genres('retired custom style','synth pop')
    assert 'Retired custom style' in client.get('/genres/list',params={'q':'Retired custom style'}).text
    options = client.get('/home/genres').json()['options']
    assert not any(row['name']=='retired custom style' for row in options)
    client.post('/genres/remove',data={'name':'Searchable custom alias'})
    assert 'Renamed style' not in store.genres.get_genre_whitelist()


def test_preference_edits_do_not_reconfigure_the_catalog(store, monkeypatch):
    client = _client(store)
    before = dict(store.conn.execute('SELECT genre_key,enabled FROM genre_preferences'))

    def unexpected_rebuild(**kwargs):
        raise AssertionError('Preference edits must only reload the matcher')

    monkeypatch.setattr(store.genres, 'configure_catalog', unexpected_rebuild)
    assert client.post('/genres/add', data={'name':'Custom sound'}).status_code == 200
    assert client.post('/genres/remove', data={'name':'rap'}).status_code == 200
    after = dict(store.conn.execute('SELECT genre_key,enabled FROM genre_preferences'))
    assert after == {**before, 'custom sound':1, 'hip hop':0}


def test_full_editor_reads_taxonomy_without_per_genre_queries(store):
    client = _client(store)
    count = store.conn.execute('SELECT count(*) FROM genre_catalog').fetchone()[0]
    assert count > 2000
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        response = client.get('/genres/list')
    finally:
        store.conn.set_trace_callback(None)
    assert response.status_code == 200 and 'Rock' in response.text
    assert response.text.count('class="genre-chip"') == count
    # The normal catalog has over 2,000 genres. A full render must not do
    # separate identity/family/redirect lookups for every label and alias.
    assert len(queries) < 30, len(queries)
