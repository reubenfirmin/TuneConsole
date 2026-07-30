"""#104 The "Songs like" modal opened off-screen.

`main` carries `animation: rise ... both`, whose keyframes animate transform, so main creates a
containing block for position:fixed even in its filled state. A fixed modal swapped in UNDER main is
therefore positioned against main, not the viewport, and lands wherever main happens to be. base.html
already documents this for #conflicts-modal, which lives outside <main> for exactly this reason;
#similar-modal did not, so it inherited the bug.
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(albums={"MPREb_x": {"title": "A", "artists": [{"name": "B"}],
                                        "thumbnails": [], "tracks": []}})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1")


def _outside_main(html: str, needle: str) -> bool:
    """Is `needle` outside the <main> element? main's transform would capture a fixed child."""
    assert needle in html, f"{needle} missing entirely"
    end_main = html.rindex("</main>")
    return html.index(needle) > end_main


def test_similar_modal_container_is_outside_main_on_a_playlist_page(store):
    pid = store.upsert_playlist(store.upsert_identity("x", "c", None, False), "PL1", "Mix", 1, "h", 1.0)
    html = _client(store).get(f"/playlist/{pid}").text
    assert _outside_main(html, 'id="similar-modal"')


def test_similar_modal_container_is_outside_main_on_an_album_page(store):
    html = _client(store).get("/album?browse=MPREb_x").text
    assert _outside_main(html, 'id="similar-modal"')


def test_similar_modal_container_is_declared_once(store):
    """It lives in base.html now, so a page must not also carry its own copy: two elements with the
    same id would make the htmx target ambiguous (it would swap into the first, inside main)."""
    html = _client(store).get("/album?browse=MPREb_x").text
    assert html.count('id="similar-modal"') == 1


def test_similar_results_absorbs_the_row_bleed():
    """#106 .alt-list uses negative horizontal margins so a row hover spans the full width. The scroll
    container must give them room, or they overhang it, and overflow-y:auto forces overflow-x to
    compute to auto: a horizontal scrollbar. Measured: scrollWidth 500 vs clientWidth 494 before."""
    css = (Path(__file__).resolve().parents[1]
           / "src/yt_playlist/web/static/app.css").read_text()
    results = re.search(r"\.modal\.modal-similar #similar-results \{([^}]*)\}", css)
    assert results, "#similar-results rule missing"
    assert "padding-inline: 0.4rem" in results.group(1)
    bleed = re.search(r"\.modal\.modal-similar \.alt-list \{([^}]*)\}", css)
    assert "-0.4rem" in bleed.group(1), "the padding above must match the bleed it absorbs"


def test_conflicts_modal_still_outside_main(store):
    """The prior art this fix follows: do not regress it."""
    html = _client(store).get("/album?browse=MPREb_x").text
    assert _outside_main(html, 'id="conflicts-modal"')
