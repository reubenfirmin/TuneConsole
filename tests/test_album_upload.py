"""#107 Albums you uploaded to YouTube Music (privately owned releases) must render a track list.
ytmusicapi's get_album() rejects any non-MPRE browse id before it ever hits the network, so these
albums have to go through the uploads endpoint instead."""
import re

from fastapi.testclient import TestClient

from yt_playlist.library.album_fetch import UPLOAD_RELEASE_PREFIX, fetch_album, is_upload_album
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

UPLOAD_ID = UPLOAD_RELEASE_PREFIX + "b_po_XYZ"


def _anchor(html: str, cls: str) -> str:
    """The opening <a ...> tag carrying `cls`. [^>] spans newlines, so this catches attributes
    wherever they wrapped to."""
    m = re.search(r'<a[^>]*class="' + cls + r'"[^>]*>', html)
    assert m, f"no <a class={cls!r}> in the rendered page"
    return m.group(0)


class _RecordingClient:
    """Records which endpoint the router picked."""
    def __init__(self):
        self.calls = []

    def get_album(self, browse_id):
        self.calls.append(("get_album", browse_id))
        return {"title": "Catalog"}

    def get_library_upload_album(self, browse_id):
        self.calls.append(("get_library_upload_album", browse_id))
        return {"title": "Upload"}


def test_is_upload_album_recognises_privately_owned_releases():
    assert is_upload_album(UPLOAD_ID) is True
    assert is_upload_album("MPREb_x") is False
    assert is_upload_album(None) is False
    assert is_upload_album("") is False


def test_fetch_album_routes_upload_to_the_uploads_endpoint():
    c = _RecordingClient()
    assert fetch_album(c, UPLOAD_ID) == {"title": "Upload"}
    assert c.calls == [("get_library_upload_album", UPLOAD_ID)]


def test_fetch_album_routes_catalog_album_to_get_album():
    c = _RecordingClient()
    assert fetch_album(c, "MPREb_x") == {"title": "Catalog"}
    assert c.calls == [("get_album", "MPREb_x")]


_UPLOAD_ALBUM = {
    "title": "Disco-tech (disc 1)", "year": "1999",
    "artists": [{"name": "Gatecrasher"}],
    "thumbnails": [{"url": "http://t/1.jpg", "width": 300, "height": 300}],
    "tracks": [{"title": "Opener", "videoId": "u1", "duration": "6:01", "artists": [{"name": "Gatecrasher"}]},
               {"title": "Closer", "videoId": "u2", "duration": "7:20", "artists": [{"name": "Gatecrasher"}]}],
}


class _StrictClient(FakeClient):
    """FakeClient whose get_album enforces the same guard the real ytmusicapi does:

        if not browseId or not browseId.startswith("MPRE"):
            raise YTMusicUserError("Invalid album browseId provided, must start with MPRE.")

    Without this the fake silently returns {} for an upload id, the route builds a truthy album dict
    with an empty title, and the #107 dead-end never reproduces.
    """
    def get_album(self, browseId):
        if not browseId or not browseId.startswith("MPRE"):
            raise ValueError("Invalid album browseId provided, must start with MPRE.")
        return super().get_album(browseId)


def _upload_client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = _StrictClient(uploads={UPLOAD_ID: _UPLOAD_ALBUM})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), fc


def test_upload_album_page_renders_tracks_not_unavailable(store):
    """The #107 regression: this browse id used to raise inside get_album (non-MPRE) and dead-end."""
    c, _fc = _upload_client(store)
    r = c.get(f"/album?browse={UPLOAD_ID}")
    assert r.status_code == 200
    assert "Album unavailable" not in r.text
    assert "Disco-tech (disc 1)" in r.text and "Gatecrasher" in r.text
    assert "Opener" in r.text and "Closer" in r.text


def test_upload_album_share_txt_lists_track_urls(store):
    c, _fc = _upload_client(store)
    r = c.get(f"/album/{UPLOAD_ID}/share.txt")
    assert r.status_code == 200
    assert "https://music.youtube.com/watch?v=u1" in r.text
    assert "https://music.youtube.com/watch?v=u2" in r.text


def test_upload_album_shows_your_upload_tag_and_no_save_button(store):
    # An upload has no audioPlaylistId to like, so "Save to library" could only ever fail here.
    c, _fc = _upload_client(store)
    r = c.get(f"/album?browse={UPLOAD_ID}")
    assert "Your upload" in r.text
    assert "/collection/save-album" not in r.text


def test_upload_album_renders_the_folded_in_library_table(store):
    """An upload IS your library (that is what "privately owned" means), so it gets the same editable
    table a saved album gets: genre, year, plays, the liked heart. Without this it fell back to the
    read-only preview table, which has none of them."""
    c, _fc = _upload_client(store)
    html = c.get(f"/album?browse={UPLOAD_ID}").text
    assert ">Genre<" in html and ">Plays<" in html
    assert "like-btn" in html


def test_upload_album_folds_all_live_tracks_into_the_library(store):
    """The fold-in must complete a PARTIAL local subset. Playlist sync stamps only the upload tracks
    that appear in a playlist (3 of 17 for a real album here), and the old `not tracks` guard skipped
    the fold entirely whenever even one row already existed, showing 3 of 17."""
    store.upsert_track("u1", "Opener", "Gatecrasher", "Disco-tech (disc 1)", None,
                       album_browse_id=UPLOAD_ID)          # one incidental row, as sync would leave
    c, _fc = _upload_client(store)
    html = c.get(f"/album?browse={UPLOAD_ID}").text
    assert "Opener" in html and "Closer" in html            # BOTH, not just the incidental one
    assert len(store.album_tracks_detail(UPLOAD_ID)) == 2   # the missing track got folded in


def test_upload_album_enrich_button_is_enabled(store):
    """Enrichment runs over folded-in tracks. Uploads have them now, so the icon must not be disabled
    (it is gated on saved-ness, which an upload can never have)."""
    c, _fc = _upload_client(store)
    html = c.get(f"/album?browse={UPLOAD_ID}").text
    enrich = re.search(r'<button[^>]*aria-label="Enrich"', html)
    assert enrich, "enrich icon missing"
    # the gate lives in the :disabled binding just before aria-label
    block = html[max(0, enrich.start() - 400):enrich.end()]
    assert "|| true" not in block


def test_enrich_tooltip_renders_a_real_ampersand(store):
    """The tooltip lives inside a Jinja {{ }} string, so it is autoescaped on the way out: writing
    &amp; there double-escapes to &amp;amp; and the tooltip literally reads "&amp;". The raw & is
    what's wanted; escaping turns it into the &amp; the HTML needs."""
    c, _fc = _upload_client(store)
    html = c.get(f"/album?browse={UPLOAD_ID}").text
    assert "&amp;amp;" not in html
    assert "year &amp; audio features" in html      # single-escaped: renders as "year & audio"


def test_album_hides_the_artist_tag_when_there_is_no_album_artist(store):
    """A compilation upload has no single album artist, so YouTube returns none. Rendering the amber
    tag anyway leaves an empty pill in the header."""
    iid = store.upsert_identity("main", "cred", None, True)
    no_artist = dict(_UPLOAD_ALBUM, artists=[])
    fc = _StrictClient(uploads={UPLOAD_ID: no_artist})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    html = TestClient(app, base_url="http://127.0.0.1").get(f"/album?browse={UPLOAD_ID}").text
    assert '<span class="tag amber"></span>' not in html


_PH = "https://www.gstatic.com/youtube/media/ytm/images/cover_track_default@1200.png?"


def test_album_with_no_cover_shows_a_mosaic_of_its_track_art(store):
    """A personal compilation has no cover of its own (YouTube serves a grey disc), but its tracks do
    once enriched. A mosaic is honest about it being a collection, where picking one track's art to
    stand for 17 different artists would not be."""
    for vid, title, art in (("u1", "Opener", "http://art/1.jpg"), ("u2", "Closer", "http://art/2.jpg")):
        store.upsert_track(vid, title, "Gatecrasher", "Disco-tech (disc 1)", 100,
                           album_browse_id=UPLOAD_ID, thumbnail=art)
    iid = store.upsert_identity("main", "cred", None, True)
    fc = _StrictClient(uploads={UPLOAD_ID: dict(_UPLOAD_ALBUM, thumbnails=[{"url": _PH}])})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    html = TestClient(app, base_url="http://127.0.0.1").get(f"/album?browse={UPLOAD_ID}").text
    assert "pl-art-mosaic" in html
    assert "http://art/1.jpg" in html and "http://art/2.jpg" in html
    assert _PH not in html                      # the grey disc is never shown once we have real art


def test_album_with_real_cover_keeps_the_single_cover(store):
    """No regression: an ordinary album with its own art shows it, not a mosaic."""
    real = "https://lh3.googleusercontent.com/real=w544"
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(albums={"MPREb_x": {"title": "A", "artists": [{"name": "B"}],
                                        "thumbnails": [{"url": real}], "tracks": []}})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    html = TestClient(app, base_url="http://127.0.0.1").get("/album?browse=MPREb_x").text
    assert real in html
    assert "pl-art-mosaic" not in html


def test_song_title_links_are_informational_but_play_buttons_are_not(store):
    """#107(b): a song title is opened to LOOK at it, so it must escape the app.js interceptor that
    hijacks the playing YouTube Music tab. The row's play button must NOT escape it."""
    c, _fc = _upload_client(store)
    html = c.get(f"/album?browse={UPLOAD_ID}").text
    # Match the whole <a ...> tag: these anchors wrap across lines, so a line-wise check would miss
    # an attribute that landed on the second line.
    assert "data-yt-info" in _anchor(html, "ptitle")
    assert "data-yt-info" not in _anchor(html, "pl-play")


def test_catalog_album_still_shows_save_button(store):
    # No regression: an ordinary unsaved album keeps its save button.
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient(albums={"MPREb_x": {"title": "The Album", "artists": [{"name": "A"}],
                                        "thumbnails": [], "tracks": []}})
    app = create_app(store, lambda: {iid: fc}, now_fn=lambda: 1.0)
    r = TestClient(app, base_url="http://127.0.0.1").get("/album?browse=MPREb_x")
    assert "/collection/save-album" in r.text
    assert "Your upload" not in r.text
