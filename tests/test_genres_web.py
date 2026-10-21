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
    n = len(g.builtin_names())
    r = c.post("/genres/add", data={"name": "Whack-a-doodle"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()   # a fragment, not the whole page
    assert "Whack-a-doodle" in r.text
    assert f"{n + 1} genres" in r.text               # count reflects the addition
    # persisted and recognized by the live matcher
    assert "Whack-a-doodle" in store.get_genre_whitelist()
    assert g.pick_genre(["seen live", "whack-a-doodle"]) == "Whack-a-doodle"


def test_remove_drops_genre(store):
    c = _client(store)
    c.post("/genres/add", data={"name": "Whack-a-doodle"})
    r = c.post("/genres/remove", data={"name": "Whack-a-doodle"})
    assert r.status_code == 200
    assert "Whack-a-doodle" not in r.text
    assert "Whack-a-doodle" not in store.get_genre_whitelist()
    assert g.pick_genre(["whack-a-doodle"]) is None


def test_reset_restores_defaults(store):
    c = _client(store)
    n = len(g.builtin_names())
    c.post("/genres/add", data={"name": "Whack-a-doodle"})
    assert len(store.get_genre_whitelist()) == n + 1
    r = c.post("/genres/reset")
    assert r.status_code == 200
    assert "Rock" in r.text and "Whack-a-doodle" not in r.text
    assert len(store.get_genre_whitelist()) == n


def test_empty_add_returns_oob_toast(store):
    c = _client(store)
    before = len(store.get_genre_whitelist())
    r = c.post("/genres/add", data={"name": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "enter a genre name" in r.text
    assert len(store.get_genre_whitelist()) == before     # nothing added


def test_equivalent_add_is_idempotent_and_alias_removal_disables_identity(store):
    c = _client(store)
    before = store.get_genre_whitelist()
    c.post("/genres/add", data={"name": "hip-hop"})
    c.post("/genres/add", data={"name": "rap"})
    assert store.get_genre_whitelist() == before
    c.post("/genres/remove", data={"name": "hip-hop"})
    assert g.pick_genre(["rap", "hiphop", "Hip Hop"]) is None


def test_search_matches_aliases_and_family_labels_without_changing_enabled_list(store):
    c = _client(store)
    before = store.get_genre_whitelist()
    alias = c.get("/genres/list", params={"q": "rap"}).text
    assert "Hip Hop" in alias and "Deep House" not in alias
    family = c.get("/genres/list", params={"q": "House"}).text
    assert "Deep House" in family and 'aria-label="Jazz"' not in family
    assert "<details open>" in family
    assert store.get_genre_whitelist() == before


def test_query_survives_edits_and_escapes_input(store):
    c = _client(store)
    r = c.post("/genres/add", data={"name": "Whack-a-doodle", "q": "Whack"})
    assert "Whack-a-doodle" in r.text and "Deep House" not in r.text
    r = c.get("/genres/list", params={"q": "<script>bad</script>"})
    assert "<script>bad</script>" not in r.text
    assert "&lt;script&gt;bad&lt;/script&gt;" in r.text
