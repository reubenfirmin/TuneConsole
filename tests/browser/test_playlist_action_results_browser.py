"""Real HTMX navigation preserves failed and partial bulk-action results."""
import pytest
from playwright.sync_api import expect

from yt_playlist.library.ops import PlaylistOps


@pytest.mark.parametrize("operation", ["copy", "delete"])
@pytest.mark.parametrize("outcome", ["failed", "partial"])
def test_bulk_action_result_survives_navigation_and_reload(live_app, store, page, monkeypatch, operation, outcome):
    first = store.playlists.get_playlists()[0]
    second = store.playlists.upsert_playlist(first.identity_id, "PLB", "Second playlist", 0, "", 0)
    calls = []
    if operation == "copy":
        def copy(*args):
            calls.append("copy")
            if outcome == "failed":
                raise RuntimeError("Response lost")
            return {"new_ytm": "NEW", "title": "My copy", "added": 2, "skipped": 1,
                    "skipped_video_ids": ["missing"]}
        monkeypatch.setattr(PlaylistOps, "copy", copy)
    else:
        def delete(self, pid):
            calls.append(pid)
            if outcome == "failed" or pid == first.id:
                raise ValueError("Delete rejected")
            store.playlists.remove_playlist(pid)
        monkeypatch.setattr(PlaylistOps, "delete", delete)
    page.goto(f"{live_app}/playlists")
    page.locator(f'.playlist-table-card tr[data-playlist-id="{first.id}"] input').check()
    if operation == "copy":
        page.get_by_role("button", name="Copy", exact=True).click()
        page.get_by_placeholder("New playlist name").fill("My copy")
        page.get_by_placeholder("New playlist name").press("Enter")
    else:
        page.locator(f'.playlist-table-card tr[data-playlist-id="{second}"] input').check()
        page.get_by_role("button", name="Delete", exact=True).click()
        page.get_by_role("button", name="Delete them", exact=True).click()
    result = page.get_by_role("alert", name="Playlist action result")
    expect(result).to_be_visible()
    expect(result).to_have_attribute("data-outcome", outcome)
    expect(result).to_contain_text("Response lost" if operation == "copy" and outcome == "failed" else
                                  "Delete rejected" if operation == "delete" else "Copied 2 track(s)")
    expect(page.locator(".pl-actionbar")).to_be_hidden()
    count = len(calls)
    page.reload()
    expect(result).to_be_visible()
    assert len(calls) == count
