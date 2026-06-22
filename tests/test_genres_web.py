"""Contract tests for the htmx genre whitelist editor (/genres + /genres/add|remove|reset).

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
    body = c.get("/genres").text
    assert "Rock" in body                       # a built-in chip is server-rendered
    assert 'id="genre-list"' in body            # the htmx swap zone is present
    assert "genresTab" not in body              # the Alpine factory is no longer referenced


def test_add_returns_fragment_with_genre(store):
    c = _client(store)
    n = len(g.builtin_names())
    r = c.post("/genres/add", data={"name": "Phonk"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()   # a fragment, not the whole page
    assert "Phonk" in r.text
    assert f"{n + 1} genres" in r.text               # count reflects the addition
    # persisted and recognized by the live matcher
    assert "Phonk" in store.get_genre_whitelist()
    assert g.pick_genre(["seen live", "phonk"]) == "Phonk"


def test_remove_drops_genre(store):
    c = _client(store)
    c.post("/genres/add", data={"name": "Phonk"})
    r = c.post("/genres/remove", data={"name": "Phonk"})
    assert r.status_code == 200
    assert "Phonk" not in r.text
    assert "Phonk" not in store.get_genre_whitelist()
    assert g.pick_genre(["phonk"]) is None


def test_reset_restores_defaults(store):
    c = _client(store)
    n = len(g.builtin_names())
    c.post("/genres/add", data={"name": "Phonk"})
    assert len(store.get_genre_whitelist()) == n + 1
    r = c.post("/genres/reset")
    assert r.status_code == 200
    assert "Rock" in r.text and "Phonk" not in r.text
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
