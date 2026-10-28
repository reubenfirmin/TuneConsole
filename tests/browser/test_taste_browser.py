"""Taste overview, playlist influence, recommendation comparison and feedback."""
import json
from urllib.parse import urlsplit

import numpy as np
import pytest
from playwright.sync_api import expect

from yt_playlist.rec import rec_params
from yt_playlist.util.matching import identity_key
from yt_playlist.web.track_links import track_url


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_preview_and_compare_show_artwork_play_tracks_and_keep_baseline(live_app, store, page, monkeypatch, width, tmp_path):
    from yt_playlist.rec import surfaces

    title = 'A song with a "long" title & another verse'
    trace = {"durable": .4, "transient_delta": .1, "session_delta": 0,
             "audio_delta": 0, "axes": {}, "contexts": [{"name": "Night drive"}]}
    sample = [surfaces.ForYouItem(title, "Artist & guest", "An album", "preview-song",
                                  "/static/nag/intro.png", 0, "In your taste neighbourhood",
                                  key="song|artist", lane="neighbourhood", trace=trace),
              surfaces.ForYouItem("Missing artwork", "Another artist", "", "missing-art",
                                  "/static/missing-art.jpg", 0, "A deep cut",
                                  key="missing|artist", lane="deep_cut"),
              surfaces.ForYouItem("Unresolved song", "Unknown artist", "", None,
                                  None, 0, "Playlist neighbor", key="unknown|artist", lane="rotation")]
    monkeypatch.setattr(surfaces, "taste_sample", lambda *a, **kw: sample)
    comparison_calls = []

    def compare(_store, _now, keys=None, limit=24):
        comparison_calls.append(keys)
        changed = rec_params.get_param(store, "breadth_bias") != 0
        first = {"key": "song|artist", "title": title, "artist": "Artist & guest",
                 "album": "An album", "video_id": "preview-song", "thumbnail": "/static/nag/intro.png",
                 "rank": 2 if changed else 1, "in_cohort": True, "score": .8,
                 "trace": trace, "lane": "neighbourhood", "lane_weight": 1,
                 "eligible": True, "eligibility_reason": None}
        old = {**first, "key": "old|artist", "title": "Former pick", "video_id": "former-pick",
               "rank": None if changed else 2, "in_cohort": not changed, "lane": "deep_cut"}
        if not changed:
            return [first, old]
        new = {**first, "key": "new|artist", "title": "New pick", "video_id": "new-pick", "rank": 1}
        return [new, first, old]

    monkeypatch.setattr(surfaces, "taste_comparison", compare)
    play_requests = []
    page.route("**/play", lambda route: (play_requests.append(route.request.post_data_json),
                                        route.fulfill(json={"ok": True})))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#experiment/preview")
    page.locator(".taste-preview-intro").scroll_into_view_if_needed()
    sample_table = page.get_by_role("table", name="Sample suggestions", exact=True)
    expect(sample_table.get_by_role("row")).to_have_count(4)
    expect(sample_table.locator(".taste-track-art img")).to_have_count(1)
    expect(sample_table.get_by_text("Playback unavailable", exact=True)).to_be_visible()
    play = sample_table.get_by_role("link", name=f"Play {title} by Artist & guest on YouTube Music", exact=True)
    assert play.bounding_box()["width"] >= 44
    play.focus()
    with page.expect_response(lambda response: urlsplit(response.url).path == "/play"):
        play.press("Enter")
    assert play_requests == [{"url": "https://music.youtube.com/watch?v=preview-song"}]
    expect(sample_table.get_by_role("link", name=title, exact=True)).to_have_attribute("href", track_url(sample[0]))

    page.locator("#comparison-body").scroll_into_view_if_needed()
    expect(page.locator("#comparison-body")).not_to_contain_text("Loading")
    page.get_by_role("button", name="Capture current picks", exact=True).click()
    expect(page.locator("#comparison-body [role=status]")).to_contain_text("Current picks captured")
    before = store.settings.get_setting("taste_comparison_baseline_v2")
    expect(page.get_by_role("table", name="Before and after suggestions").get_by_role("row")).to_have_count(3)
    page.get_by_role("tab", name="Starting point", exact=True).click()
    expect(page.get_by_role("heading", name="Your starting point", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Preview & compare", exact=True).click()
    expect(page.get_by_role("table", name="Before and after suggestions").get_by_role("row")).to_have_count(3)
    assert store.settings.get_setting("taste_comparison_baseline_v2") == before
    page.get_by_role("link", name="Main controls", exact=True).click()
    slider = page.locator("#param-breadth_bias")
    slider.evaluate("el => { el.value = '.5'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    expect(page.locator("#taste-save-status")).to_contain_text("Change saved")
    page.get_by_role("tab", name="Experiment", exact=True).click()
    expect(page.locator(".sample-note")).to_contain_text("Settings changed")
    page.get_by_role("button", name="Compare changes", exact=True).click()
    table = page.get_by_role("table", name="Before and after suggestions")
    expect(table.get_by_role("row")).to_have_count(4)
    expect(table.locator(".taste-rank-change")).to_have_text(["NEW", "Down 1", "OUT"])
    expect(table.locator(".taste-track-rank")).to_have_text(["Not captured", "#1", "#1", "#2", "#2", "Outside top 24"])
    assert store.settings.get_setting("taste_comparison_baseline_v2") == before
    assert comparison_calls[-1] == ["song|artist", "old|artist"]
    with page.expect_response(lambda response: urlsplit(response.url).path == "/play"):
        table.get_by_role("link", name="Play Former pick by Artist & guest on YouTube Music", exact=True).click()
    assert play_requests[-1] == {"url": "https://music.youtube.com/watch?v=former-pick"}
    assert page.url.endswith("#experiment/preview")
    assert len(page.context.pages) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".taste-comparison").screenshot(path=str(tmp_path / f"taste-comparison-{width}.png"))
    page.get_by_role("button", name="Refresh sample", exact=True).click()
    expect(page.locator(".sample-note")).to_contain_text("Sample updated")
    page.locator(".foryou-sample").screenshot(path=str(tmp_path / f"taste-preview-{width}.png"))
    page.get_by_role("tablist", name="Experiment tools", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"taste-experiment-subtabs-{width}.png"))
    assert errors == []


def test_overview_updates_itself_when_background_genre_repair_finishes(store, page, monkeypatch, request):
    from yt_playlist.rec import embedding_vectors as embed
    from yt_playlist.rec.rec_worker import RecWorker
    repairing = {"busy": True}
    monkeypatch.setattr(RecWorker, "trigger_mode_refresh", lambda self, **kw: None)
    monkeypatch.setattr(RecWorker, "busy", property(lambda self: repairing["busy"]))
    live_app = request.getfixturevalue("live_app")
    iid = store.identities.get_identities()[0].id
    store.settings.set_setting("rec_content_model", json.dumps({"cat": {"fam:techno": 0}, "ncat": 1, "cont": []}))
    keys = []
    for i in range(3):
        tid = store.tracks.upsert_track(f"repair-{i}", f"Repair {i}", "Artist", None, None)
        store.tracks.set_track_genre(tid, "Techno")
        keys.append(f"repair {i}|artist")
    store.rec_model.replace_rec_content_vectors([(key, np.ones(1, dtype=np.float32).tobytes()) for key in keys])
    store.history.import_play_events(iid, [(key, f"repair-{i}", 1.0) for i, key in enumerate(keys)])
    mode = {"mode_id": 1, "label": "Techno", "families": [["techno", 3]],
            "centroid": np.ones(1, dtype=np.float32), "size": 3, "rep_keys": keys, "space": "old"}
    store.modes.replace_modes([mode], retired_ids=[], now=1.0)
    page.goto(live_app + "/taste")
    overview = page.locator("#taste-overview-panel")
    expect(overview.get_by_role("status")).to_contain_text("Updating your music groups automatically")
    mode["space"] = embed.content_space_id(store)
    store.modes.replace_modes([mode], retired_ids=[], now=2.0)
    repairing["busy"] = False
    expect(overview.locator(".taste-shift-row")).to_have_count(1, timeout=7000)
    expect(overview.locator('[hx-trigger="every 3s"]')).to_have_count(0)
    expect(overview.locator(".taste-status-line")).to_have_count(1)
    expect(overview.locator(".taste-summary")).to_have_count(3)
    expect(page.locator("#taste-profile-panel .taste-panel-content")).to_contain_text("Loading taste profile")


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_populated_overview_leads_with_changes_and_keeps_feedback_compact(store, page, monkeypatch, request, width, tmp_path):
    from yt_playlist.rec.rec_worker import RecWorker

    monkeypatch.setattr(RecWorker, 'trigger_mode_refresh', lambda self, **kw: None)
    live_app = request.getfixturevalue('live_app')
    iid = store.identities.get_identities()[0].id
    store.modes.replace_modes([{
        'mode_id': 1, 'label': 'Latin', 'families': [['latin', 1]],
        'centroid': np.ones(1, dtype=np.float32), 'size': 3, 'rep_keys': [],
    }], retired_ids=[], now=1.0)
    store.history.record_history_plays(iid, 1.0, ['song|artist'], source_timezone='UTC')
    for i in range(47):
        store.rec_model.record_like(f'action-{i}|artist', 1.0, provenance='action')
    for i in range(85):
        store.rec_model.record_like(f'sync-{i}|artist', 1.0, provenance='sync')
    for i in range(3):
        store.rec_model.record_dislike(f'disliked-{i}|artist', until=None, now=1.0)
    for axis in ('genre:latin', 'era:2010', 'artist:Hermanos Gutiérrez'):
        store.rec_signals.log_graduation(axis, 'like', 2, 1.1, 1.1, 1.0)

    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/taste')
    recent = page.locator('.taste-recent-summary')
    expect(recent.locator('.taste-preference-name strong')).to_have_text(['Hermanos Gutiérrez', '2010s', 'latin'])
    expect(recent.locator('.taste-preference-axis')).to_have_text(['Artist', 'Era', 'Genre'])
    expect(recent.locator('.taste-preference-direction')).to_have_text(['Increased'] * 3)
    expect(recent.locator('.taste-feedback-stats dd')).to_have_text(['47', '85', '3'])
    expect(recent.get_by_text('Found by sync', exact=True)).to_be_visible()
    expect(recent.locator('.taste-recent-listening')).to_contain_text('precise times aren’t available')
    expect(recent.locator('.taste-recent-listening time')).to_have_attribute('datetime', '1970-01-01')
    expect(recent.locator('.taste-feedback-note')).not_to_have_attribute('open', '')
    expect(recent.locator('.taste-feedback-note p')).to_be_hidden()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    preferences = recent.locator('.taste-recent-preferences').bounding_box()
    feedback = recent.locator('.taste-recent-feedback').bounding_box()
    if width > 900:
        assert feedback['x'] >= preferences['x'] + preferences['width']
        assert recent.bounding_box()['height'] < 550
    else:
        assert feedback['y'] >= preferences['y'] + preferences['height']
    page.screenshot(path=str(tmp_path / f'taste-overview-populated-{width}.png'), full_page=True)
    summary = recent.locator('.taste-feedback-note summary')
    summary.focus()
    summary.press('Enter')
    expect(recent.locator('.taste-feedback-note p')).to_be_visible()
    expect(recent.locator('.taste-feedback-note p')).to_contain_text('original rating dates aren’t known')
    recent.get_by_role('link', name='See learning activity').click()
    expect(page.get_by_role('tab', name='Recent & learning', exact=True)).to_have_attribute('aria-selected', 'true')


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
def test_playlist_influence_shows_the_whole_blend_and_links_selected_rows(live_app, store, page, width, tmp_path):
    iid = store.identities.get_identities()[0].id
    titles = ['Night drives / broken beats & "other" late discoveries',
              "Sunday slow / piano, soul and a little rain"]
    counts = [34, 22, 14, 10, 6, 5, 4, 2, 2, 1, 0]
    vectors, pids = [], []
    for i, count in enumerate(counts):
        title = titles[i] if i < len(titles) else f"Listening room {i + 1}"
        tracks = []
        for j, genre in enumerate(("Techno" if i % 2 == 0 else "Folk", "Jazz")):
            tid = store.tracks.upsert_track(f"influence-{i}-{j}", f"Blend {i} {j}", "Artist", None, None)
            store.tracks.set_track_genre(tid, genre)
            tracks.append(tid)
            vectors.append((f"blend {i} {j}|artist", np.array([1, i + 1], dtype=np.float32).tobytes()))
        pid = store.playlists.upsert_playlist(iid, f"influence-{i}", title, 2, f"influence-{i}", 0)
        store.playlists.set_playlist_tracks(pid, tracks)
        pids.append(pid)
        for play in range(count):
            store.history.record_history_plays(iid, -play * 86400.0,
                                               [f"blend {i} 0|artist"], source_timezone="UTC")
    store.rec_model.replace_rec_vectors(vectors)
    store.settings.set_setting("rec_dim", "2")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(live_app + "/taste#profile")
    influence = page.locator("#taste-playlist-influence")
    influence.scroll_into_view_if_needed()
    rows = influence.locator(".playlist-influence-row")
    expect(rows).to_have_count(9)
    expect(influence.locator(".playlist-blend-segment")).to_have_count(9)
    expect(influence.get_by_text("10 interests shape this match", exact=True)).to_be_visible()
    expect(influence.locator(".playlist-blend-caption")).to_contain_text("70% comes from your top 3")
    expect(rows.first.locator(".playlist-influence-title")).to_have_text(titles[0])
    expect(rows.first.locator(".playlist-influence-plays")).to_contain_text("34")
    expect(rows.first.locator(".playlist-influence-genres")).to_contain_text("techno")
    expect(rows.last.locator(".playlist-influence-title")).to_have_text("Other 2 playlists")
    expect(rows.last.locator(".playlist-influence-share")).to_contain_text("3%")
    expect(influence.locator(".playlist-influence-footnote")).to_contain_text("1 context has no allocation weight yet")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    influence.locator("..").screenshot(path=str(tmp_path / f"playlist-blend-{width}.png"))

    ring = influence.locator(".playlist-blend-ring")
    ring.scroll_into_view_if_needed()
    bounds = ring.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] * .75, bounds["y"] + bounds["height"] * .25)
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("34%")
    page.mouse.move(0, 0)
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("100%")
    rows.first.click()
    page.mouse.move(0, 0)
    expect(rows.first).to_have_attribute("aria-pressed", "true")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("34%")
    expect(influence.locator(".playlist-blend-segment.is-muted")).to_have_count(8)
    expect(influence.get_by_role("link", name="Open playlist", exact=True).filter(visible=True)).to_have_attribute("href", f"/playlist/{pids[0]}")
    influence.locator("..").screenshot(path=str(tmp_path / f"playlist-blend-selected-{width}.png"))
    rows.nth(1).focus()
    rows.nth(1).press("Enter")
    expect(rows.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("22%")
    rows.nth(1).press("Escape")
    expect(influence.locator(".playlist-blend-center strong")).to_have_text("100%")
    expect(influence.locator(".playlist-blend-segment.is-muted")).to_have_count(0)
    assert errors == []


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_feedback_uses_track_table_with_artwork_sorting_and_unban(live_app, store, page, width, tmp_path):
    title = 'Alpha "Live" & the extended recording'
    artist = "Artist's Band"
    key = identity_key(title, artist)
    store.tracks.upsert_track("feedback-alpha", title, artist, "First Album", None,
                       thumbnail="/static/nag/intro.png")
    store.tracks.upsert_track("feedback-zeta", "Zeta Song", "Another Artist", "Last Album", None,
                       thumbnail="/static/nag/trends.png")
    store.rec_model.record_dislike(key, until=2_000_000_000, now=2.0)
    store.rec_model.record_dislike("zeta song|another artist", until=None, now=3.0)
    store.rec_model.record_dislike("removed song|former artist", until=None, now=1.0)
    store.rec_model.record_like("liked|artist", now=1.0)
    store.rec_model.mark_graduated_once("liked|artist", "like_grad", now=1.0)
    for kind in ("dismiss", "not_now", "more", "less", "mute"):
        store.rec_model.record_feedback("for_you", kind + "|artist", kind, now=1.0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_app + "/taste#feedback")
    table = page.get_by_role("table", name="Banned tracks", exact=False)
    expect(table).to_be_visible()
    rows = table.locator("tbody tr.srow")
    expect(rows).to_have_count(3)
    expect(rows.first).to_contain_text("Zeta Song")
    expect(rows.nth(1).get_by_role("link", name="Artist's Band", exact=True)).to_have_attribute("href", "/artist?name=Artist%27s%20Band")
    assert table.locator("img.pl-thumb").evaluate_all("images => images.every(img => img.complete && img.naturalWidth > 0)")
    expect(table.locator("img.pl-thumb")).to_have_count(2)
    table.get_by_role("button", name="Song", exact=True).click()
    expect(rows.first).to_contain_text(title)
    table.get_by_role("button", name="Song", exact=True).press("Enter")
    expect(rows.first).to_contain_text("Zeta Song")
    expect(page.get_by_text("Liked tracks", exact=True)).to_be_visible()
    expect(page.get_by_text("Snoozed suggestions", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"taste-feedback-{width}.png"), full_page=True)
    page.get_by_role("button", name=f"Unban {title}", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(table.locator("tbody tr.srow")).to_have_count(2)
    assert key not in store.rec_model.disliked_identity_keys()
    expect(page.get_by_role("tab", name="Feedback", exact=True)).to_have_attribute("aria-selected", "true")
    page.get_by_role("button", name="Clear feedback and recommendation history", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm", exact=True).click()
    expect(table.get_by_text("No banned tracks.", exact=True)).to_be_visible()
    assert not store.rec_model.disliked_identity_keys()
    assert errors == []


