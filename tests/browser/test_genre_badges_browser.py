"""Genre labels clip before the badge padding and keep their complete border."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app


def assert_badge_padding(badge, genre, *, clipped=False):
    expect(badge).to_have_text(genre)
    expect(badge).to_have_attribute("title", genre)
    expect(badge).to_be_visible()
    label = badge.locator(".gtag-label")
    expect(label).to_have_text(genre)
    geometry = badge.evaluate("""badge => {
        const rect = badge.getBoundingClientRect();
        const label = badge.querySelector('.gtag-label');
        const text = label.getBoundingClientRect();
        const chrome = getComputedStyle(badge);
        const cell = badge.closest('td');
        const bounds = cell.getBoundingClientRect();
        const style = getComputedStyle(cell);
        return {left: rect.left, right: rect.right, cellLeft: bounds.left + parseFloat(style.paddingLeft),
            cellRight: bounds.right - parseFloat(style.paddingRight), width: rect.width,
            leftInset: text.left - rect.left - parseFloat(chrome.borderLeftWidth),
            rightInset: rect.right - text.right - parseFloat(chrome.borderRightWidth),
            paddingLeft: parseFloat(chrome.paddingLeft), paddingRight: parseFloat(chrome.paddingRight),
            clipped: label.scrollWidth > label.clientWidth};
    }""")
    assert geometry["width"] > 20
    assert geometry["left"] >= geometry["cellLeft"] - 1
    assert geometry["right"] <= geometry["cellRight"] + 1, geometry
    # The clipping element itself must stop before both padded edges, including under overflow.
    assert geometry["paddingRight"] >= 4, geometry
    assert geometry["leftInset"] == pytest.approx(geometry["paddingLeft"], abs=1), geometry
    assert geometry["rightInset"] == pytest.approx(geometry["paddingRight"], abs=1), geometry
    assert geometry["leftInset"] == pytest.approx(geometry["rightInset"], abs=1), geometry
    if clipped:
        assert geometry["clipped"], geometry
    expect(label).to_have_css("overflow-x", "hidden")
    expect(label).to_have_css("text-overflow", "clip")
    expect(badge).to_have_css("border-right-width", "1px")


@pytest.mark.parametrize("surface", ["playlist", "album", "radio"])
@pytest.mark.parametrize("width,root_size", [(860, 16), (1280, 16), (2048, 16), (2048, 28)])
def test_genre_badge_fits_its_column(song_app, page, monkeypatch, tmp_path, surface, width, root_size):
    store, source = song_app["store"], song_app["source"]
    genres = {"desert-rock": "Desert Rock", "stoner-rock": "Stoner Rock",
              "long-genre": "Progressive House with a very long genre label"}
    tracks = []
    for video, genre in genres.items():
        track = store.tracks.upsert_track(video, "Song", "Artist", "Album", 270,
                                         album_browse_id="MPRE_badge")
        store.tracks.set_track_genre(track, genre)
        tracks.append(track)
    store.playlists.set_playlist_tracks(source, tracks)
    store.collection.add_saved_album({"browse": "MPRE_badge", "title": "Album", "artist": "Artist"})
    if surface == "radio":
        monkeypatch.setattr(song_app["app"].state.ctx.radio, "status", lambda: {
            "local_id": source, "playlist_id": "SOURCE", "history_count": len(tracks), "title": "Radio Session"})
        monkeypatch.setattr(song_app["app"].state.ctx.radio, "history_tracks",
                            lambda state: store.playlists.get_playlist_tracks(source))
    page.set_viewport_size({"width": width, "height": 850})
    path = "/album?browse=MPRE_badge" if surface == "album" else f"/playlist/{source}"
    page.goto(song_app["base"] + path)
    if root_size != 16:
        # Also cover the large desktop text visible in the user's screenshot.
        page.add_style_tag(content=f"html {{ font-size: {root_size}px; }}")
    if surface == "radio":
        page.add_style_tag(url=song_app["base"] + "/static/radio.css")
        page.evaluate("() => htmx.ajax('GET', '/radio/playlist', {target: '.library-table-card', swap: 'outerHTML'})")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    for video, genre in genres.items():
        badge = page.locator(f'tr[data-vid="{video}"] .gtag')
        assert_badge_padding(badge, genre, clipped=video == "long-genre")
    page.locator("table.track-table").screenshot(
        path=tmp_path / f"genre-badges-{surface}-{width}-{root_size}.png")

    # A later metadata edit must recreate the same content-box clipping, with literal text.
    updated = 'Progressive Rock <live> & "extended" with a very long genre label'
    page.evaluate("""genre => applySongMetadata('long-genre', {
        title: 'Song', artist: 'Artist', genre, year: '2000'
    })""", updated)
    assert_badge_padding(page.locator('tr[data-vid="long-genre"] .gtag'), updated, clipped=True)


def test_enrichment_badge_keeps_its_padding(song_app, page):
    genre = 'Progressive Rock <live> & "extended" with a very long genre label'
    page.route("**/playlist/*/cells", lambda route: route.fulfill(json={
        "cells": [{"video_id": "owned-song", "genre": genre}], "pending": 0}))
    page.set_viewport_size({"width": 860, "height": 850})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    badge = page.locator('tr[data-vid="owned-song"] .gtag')
    assert_badge_padding(badge, genre, clipped=True)
