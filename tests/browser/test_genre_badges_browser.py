"""Genre labels use available space and clip before the badge padding when constrained."""
import json

import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app
from tests.onboarding_support import publish_personal_mix


def assert_badge_padding(badge, genre, *, clipped=False, full_label=False):
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
            labelWidth: text.width, naturalWidth: label.scrollWidth,
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
    if full_label:
        assert not geometry["clipped"], geometry
    # Test the actual cell space, rather than assuming a desktop viewport implies room.
    required = geometry["naturalWidth"] + geometry["paddingLeft"] + geometry["paddingRight"] + 2
    if required <= geometry["cellRight"] - geometry["cellLeft"]:
        assert not geometry["clipped"], geometry
    expect(label).to_have_css("overflow-x", "hidden")
    expect(label).to_have_css("text-overflow", "clip")
    expect(badge).to_have_css("border-right-width", "1px")


@pytest.mark.parametrize("surface", ["playlist", "album", "radio"])
@pytest.mark.parametrize("width,root_size", [(860, 16), (1280, 16), (2048, 16), (2048, 28)])
def test_genre_badge_fits_its_column(song_app, page, monkeypatch, tmp_path, surface, width, root_size):
    store, source = song_app["store"], song_app["source"]
    genres = {"desert-rock": "Desert Rock", "stoner-rock": "Stoner Rock",
              "acid-trance": "Acid Trance", "progressive-trance": "Progressive Trance",
              "long-genre": "Progressive House with a very long genre label " * 4}
    tracks = []
    for video, genre in genres.items():
        track = store.tracks.upsert_track(video, "Song", "Artist", "Album", 270,
                                         album_browse_id="MPRE_badge")
        store.tracks.set_track_genre(track, genre)
        tracks.append(track)
    store.playlists.set_playlist_tracks(source, tracks)
    store.collection.add_saved_album({"browse": "MPRE_badge", "title": "Album", "artist": "Artist"})
    if surface == "radio":
        store.settings.set_setting("last_sync_at", "1")
        store.settings.set_setting("onboard_dismissed", "1")
        publish_personal_mix(store, now=100)
        history = store.charts.playlist_tracks_detail(source)
        state = {"active": True, "starting": False, "local_id": source,
                 "playlist_id": "SOURCE", "history_count": len(history), "history": history,
                 "tracks": history, "title": "Radio Session", "directions": [], "exhausted": True}
        radio = song_app["app"].state.ctx.radio
        monkeypatch.setattr(radio, "state", lambda: state)
        monkeypatch.setattr(radio, "status", lambda: state)
        monkeypatch.setattr(radio, "prepare", lambda: None)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 850})
    path = {"album": "/album?browse=MPRE_badge", "radio": "/#radio",
            "playlist": f"/playlist/{source}"}[surface]
    page.goto(song_app["base"] + path)
    if root_size != 16:
        # Also cover the large desktop text visible in the user's screenshot.
        page.add_style_tag(content=f"html {{ font-size: {root_size}px; }}")
    if surface == "radio":
        # Exercise the production refresh and fragment swap inside its real Alpine owner.
        expect(page.locator(".radio-track-scroll .radio-history")).to_be_visible()
        state["metadata_revision"] = 1
        with page.expect_response("**/radio/playlist") as response:
            page.evaluate("""async () => {
                await Alpine.$data(document.querySelector('.radio-home')).refresh();
            }""")
        assert response.value.ok
        expect(page.locator(".radio-track-scroll .radio-history")).to_be_visible()
    page.evaluate("() => document.fonts.ready.then(() => true)")
    # Recovering the cell's outer spacing must not move any column boundaries.
    boundaries = page.locator('tr[data-vid="progressive-trance"]').evaluate("""row => {
        const bounds = () => Array.from(row.cells, cell => {
            const rect = cell.getBoundingClientRect();
            return {left: rect.left, right: rect.right};
        });
        const current = bounds(), genre = row.querySelector('.mb-genre');
        const original = genre.style.paddingInline;
        genre.style.paddingInline = getComputedStyle(genre.previousElementSibling).paddingInline;
        const withOuterSpacing = bounds();
        genre.style.paddingInline = original;
        return {current, withOuterSpacing};
    }""")
    assert boundaries["current"] == boundaries["withOuterSpacing"], boundaries
    geometry = page.locator('tr[data-vid="progressive-trance"]').evaluate("""row => ({
        cells: Array.from(row.cells, cell => ({text: cell.innerText,
            width: cell.getBoundingClientRect().width})),
        ancestors: Array.from((function* () {
            let element = row.querySelector('.gtag-label');
            while (element && element !== row) { yield element; element = element.parentElement; }
        })(), element => {
            const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
            return {tag: element.tagName, class: element.className,
                left: rect.left, right: rect.right, width: rect.width,
                scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
                display: style.display, boxSizing: style.boxSizing,
                paddingLeft: style.paddingLeft, paddingRight: style.paddingRight,
                fontSize: style.fontSize, maxWidth: style.maxWidth};
        }),
        label: {natural: row.querySelector('.gtag-label').scrollWidth,
            actual: row.querySelector('.gtag-label').clientWidth},
        viewport: innerWidth, table: row.closest('table').getBoundingClientRect().width
    })""")
    (tmp_path / "genre-geometry.json").write_text(json.dumps(geometry, indent=2))
    table = page.locator(".radio-track-scroll table.track-table" if surface == "radio" else "table.track-table")
    table.screenshot(
        path=tmp_path / f"genre-badges-{surface}-{width}-{root_size}.png")
    for video, genre in genres.items():
        badge = page.locator(f'tr[data-vid="{video}"] .gtag')
        assert_badge_padding(badge, genre, clipped=video == "long-genre",
                             full_label=(video == "progressive-trance" and surface == "radio"
                                         and (width, root_size) == (2048, 16)))

    # A later metadata edit must recreate the same content-box clipping, with literal text.
    page.evaluate("""() => applySongMetadata('progressive-trance', {
        title: 'Song', artist: 'Artist', genre: 'Progressive Trance', year: '1999'
    })""")
    assert_badge_padding(page.locator('tr[data-vid="progressive-trance"] .gtag'),
                         "Progressive Trance", full_label=(surface == "radio"
                         and (width, root_size) == (2048, 16)))
    updated = 'Progressive Rock <live> & "extended" with a very long genre label ' * 4
    page.evaluate("""genre => applySongMetadata('long-genre', {
        title: 'Song', artist: 'Artist', genre, year: '2000'
    })""", updated)
    assert_badge_padding(page.locator('tr[data-vid="long-genre"] .gtag'), updated, clipped=True)
    assert not errors, errors


def test_enrichment_badge_keeps_its_padding(song_app, page):
    genre = 'Progressive Rock <live> & "extended" with a very long genre label'
    page.route("**/playlist/*/cells", lambda route: route.fulfill(json={
        "cells": [{"video_id": "owned-song", "genre": genre}], "pending": 0}))
    page.set_viewport_size({"width": 860, "height": 850})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    badge = page.locator('tr[data-vid="owned-song"] .gtag')
    assert_badge_padding(badge, genre, clipped=True)
