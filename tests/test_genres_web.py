"""Contract tests for Enrichment's genre editor and /genres/add|remove|reset.

Fast TestClient-level assertions: routes consume form data and return the
_partials/genre_list.html fragment that htmx swaps into #genre-list; the empty-add
error path returns a 422 OOB toast. (Live behavior lives in the browser suite.)

Coverage moved here from the old JSON-based test_web.py::test_genres_whitelist_editor.
"""
from fastapi.testclient import TestClient

import yt_playlist.providers.genres as g
from yt_playlist.web.app import create_app


def _client(store):
    store.genres.configure_catalog()
    # base_url is local so state-changing POSTs pass the cross-origin guard.
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")


def test_page_renders_chip_list_zone(store):
    c = _client(store)
    redirect = c.get("/genres", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/enrich?advanced=genres#recognized-genre-tags"
    body = c.get(redirect.headers["location"]).text
    assert "<h1>Enrichment</h1>" in body
    assert "Recognized genre tags" in body
    assert "Rock" in body                       # a built-in chip is server-rendered
    assert 'id="genre-list"' in body            # the htmx swap zone is present
    assert "genresTab" not in body              # the Alpine factory is no longer referenced


def test_add_returns_fragment_with_genre(store):
    c = _client(store)
    n = len(store.genres.get_genre_whitelist())
    r = c.post("/genres/add", data={"name": "Whack-a-doodle"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()   # a fragment, not the whole page
    assert "Whack-a-doodle" in r.text
    assert f"{n + 1} genres" in r.text               # count reflects the addition
    # persisted and recognized by the live matcher
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


def test_empty_add_returns_oob_toast(store):
    c = _client(store)
    before = len(store.genres.get_genre_whitelist())
    r = c.post("/genres/add", data={"name": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "enter a genre name" in r.text
    assert len(store.genres.get_genre_whitelist()) == before     # nothing added


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


def test_query_survives_edits_and_escapes_input(store):
    c = _client(store)
    r = c.post("/genres/add", data={"name": "Whack-a-doodle", "q": "Whack"})
    assert "Whack-a-doodle" in r.text and "Deep House" not in r.text
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
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        response = client.get('/genres/list')
    finally:
        store.conn.set_trace_callback(None)
    assert response.status_code == 200 and 'Rock' in response.text
    # The normal catalog has over 2,000 genres. A full render must not do
    # separate identity/family/redirect lookups for every label and alias.
    assert len(queries) < 30, len(queries)
