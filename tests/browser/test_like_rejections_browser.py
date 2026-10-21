"""422 toasts retain HTMX failure status so optimistic hearts can roll back."""
import pytest
from playwright.sync_api import expect

from yt_playlist.library.ops import PlaylistOps


@pytest.mark.parametrize("liked", [False, True])
@pytest.mark.parametrize("failure", ["validation", "remote", None])
def test_heart_completion_preserves_server_outcome(live_app, store, page, monkeypatch, liked, failure):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    iid = store.get_identities()[0].id
    pid = store.get_playlists()[0].id
    store.upsert_playlist(iid, "LM", "Liked Music", 0, "", 0)
    entry = store.upsert_track("rating", "Rating song", "Artist", None, 200)
    store.set_playlist_tracks(pid, [entry])
    store.set_song_liked(iid, "rating", liked)
    if failure:
        def reject(self, video_id, on):
            if failure == "validation":
                raise ValueError("Rating rejected")
            raise RuntimeError("Remote failure")
        monkeypatch.setattr(PlaylistOps, "set_liked", reject)
    page.goto(f"{live_app}/playlist/{pid}")
    page.evaluate("""() => {
        window.ratingCompletions = [];
        document.addEventListener('htmx:afterRequest', event => {
            if (event.detail.xhr.responseURL.endsWith('/track/like'))
                window.ratingCompletions.push({successful: event.detail.successful,
                    failed: event.detail.failed});
        });
    }""")
    heart = page.locator('tr[data-vid="rating"] .like-btn')
    expect(heart).to_have_attribute("aria-pressed", str(liked).lower())
    with page.expect_response(lambda response: response.url.endswith("/track/like")) as response:
        heart.click()
    assert response.value.status == (422 if failure else 200)
    page.wait_for_function("window.ratingCompletions.length === 1")
    expect(heart).to_have_attribute("aria-pressed", str(liked if failure else not liked).lower())
    assert page.evaluate("window.ratingCompletions[0]") == {"successful": not bool(failure), "failed": bool(failure)}
    assert store.playlist_tracks_detail(pid)[0]["liked"] == (liked if failure else not liked)
    if failure:
        expect(page.locator("#toasts .toast-err")).to_be_visible()
        expect(page.locator("#toasts")).to_contain_text(
            "Rating rejected" if failure == "validation" else "YouTube wouldn’t update your Liked Music.")
    assert page_errors == []
