"""Explicit live acceptance: changes one continuation in the user's running Radio Session.

Normal test runs skip this check. It uses the real backend and extension observer,
resumes a paused session, leaves playback running, and retains the saved/native/page
state through a natural transition to the replacement song. No mocked Music state
or playback navigation.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest


BASE_URL = "http://127.0.0.1:8765"


def test_live_continuation_replaces_saved_suffix_and_is_played(request, tmp_path):
    if not request.config.getoption("--run-live-radio"):
        pytest.skip("Use --run-live-radio with an active, signed-in Radio Session")
    trace_path = Path(request.config.getoption("--live-radio-trace") or tmp_path / "radio-live-switch.json")
    trace = {"started_at": datetime.now(timezone.utc).isoformat(), "base_url": BASE_URL,
             "events": [], "passed": False}

    def record(kind, **data):
        trace["events"].append({"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **data})

    def call(path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json", "Origin": BASE_URL} if body is not None else {}
        with urlopen(Request(BASE_URL + path, data=body, headers=headers), timeout=10) as response:
            return json.load(response)

    def state():
        result = call("/radio/status")
        browser = result.get("browser") or {}
        native = browser.get("native") or {}
        record("observation", state={
            **{key: result.get(key) for key in (
                "active", "playlist_id", "local_id", "position", "history_count", "ahead",
                "menu_id", "revision", "connected", "starting", "updating", "error",
                "observer_error", "browser_age")},
            "saved_tracks": [{key: track.get(key) for key in ("video_id", "title", "artist")}
                             for track in result.get("tracks", [])],
            "directions": [{"id": branch["id"], "slot": branch["slot"], "active": branch["active"],
                            "video_ids": [track["video_id"] for track in branch["tracks"]]}
                           for branch in result.get("directions", [])],
            "browser": {**{key: browser.get(key) for key in (
                "observerVersion", "expected", "player", "playback_synced", "page_synced",
                "pageVideoIds", "sidebarLinkFound", "autoplay", "refresh", "refreshRequest", "viewRefresh")},
                "native": {key: native.get(key) for key in (
                    "available", "videoIds", "playlistIds", "selectedIndex", "autoplay", "autoPlaying")}},
        })
        return result

    def wait_for(check, description, timeout):
        deadline = time.monotonic() + timeout
        announced_at = 0
        while time.monotonic() < deadline:
            try:
                current = state()
                if check(current):
                    return current
            except (URLError, TimeoutError) as exc:
                record("unavailable", reason=str(exc))
            if time.monotonic() - announced_at >= 15:
                print(f"Waiting for {description}", flush=True)
                announced_at = time.monotonic()
            time.sleep(1)
        pytest.fail(f"Timed out waiting for {description}; trace: {trace_path}")

    def ready(current):
        browser = current.get("browser") or {}
        player = browser.get("player") or {}
        ids = [track["video_id"] for track in current.get("tracks", [])]
        video = player.get("video_id")
        return (current.get("active") and current.get("connected")
                and not current.get("starting") and not current.get("updating")
                and not current.get("error") and not current.get("observer_error")
                and current.get("browser_age") is not None and current["browser_age"] < 8
                and browser.get("playback_synced") and browser.get("page_synced")
                and browser.get("sidebarLinkFound") and browser.get("autoplay") == "off"
                and not player.get("paused") and video in ids and current.get("ahead") == 1
                and player.get("duration") and player["duration"] - player.get("position", 0) > 90
                and any(not branch["active"] and branch["tracks"]
                        and branch["tracks"][0]["video_id"] not in ids
                        for branch in current.get("directions", [])))

    try:
        def observed(current):
            player = (current.get("browser") or {}).get("player") or {}
            age = current.get("browser_age")
            return (current.get("active") and current.get("connected")
                    and age is not None and age < 8
                    and player.get("playlist_ytm_id") == current.get("playlist_id")
                    and player.get("video_id") in [track["video_id"] for track in current.get("tracks", [])]
                    and isinstance(player.get("paused"), bool))

        initial = wait_for(observed, "a fresh observation of the active Radio Session", 120)
        if initial["browser"]["player"]["paused"]:
            record("resume_requested", player=initial["browser"]["player"])
            response = call("/now-playing/toggle", {})
            assert response.get("ok"), response.get("error")

        before = wait_for(ready, "a settled Radio Session with time to switch", 300)
        player_before = before["browser"]["player"]
        playing = player_before["video_id"]
        ids_before = [track["video_id"] for track in before["tracks"]]
        history = ids_before[:ids_before.index(playing) + 1]
        old_future = ids_before[len(history):]
        branch = next(branch for branch in before["directions"]
                      if not branch["active"] and branch["tracks"]
                      and branch["tracks"][0]["video_id"] not in ids_before)
        replacement = branch["tracks"][0]["video_id"]
        desired = history + [replacement]
        record("choice", playlist_id=before["playlist_id"], proposal_id=branch["id"],
               menu_id=before["menu_id"], history=history, old_future=old_future, desired=desired)
        response = call("/radio/select", {"proposal_id": branch["id"], "menu_id": before["menu_id"]})
        assert response.get("ok"), response.get("reason")

        def adopted(current):
            browser = current.get("browser") or {}
            player = browser.get("player") or {}
            ids = [track["video_id"] for track in current.get("tracks", [])]
            age = current.get("browser_age")
            assert current.get("playlist_id") == before["playlist_id"], "The session playlist changed"
            assert ids[:len(history)] == history, "The saved playback history changed"
            if age is None or age >= 8 or not player.get("video_id"):
                return False
            assert player.get("video_id") == playing, "The current song changed during suffix replacement"
            return (not current.get("updating") and ids == desired and age is not None and age < 8
                    and browser.get("playback_synced") and browser.get("page_synced")
                    and (browser.get("native") or {}).get("videoIds") == desired
                    and browser.get("pageVideoIds") == desired)

        after = wait_for(adopted, "saved, native and visible playlist adoption", 90)
        browser = after["browser"]
        assert not after["error"] and not after["observer_error"]
        assert not browser["player"]["paused"]
        assert browser["player"]["position"] >= player_before["position"] - 2, "Playback sought backwards"
        assert browser["expected"]["videoIds"] == desired
        assert browser["native"]["autoplay"] is False and browser["autoplay"] == "off"
        assert browser["sidebarLinkFound"]
        assert next(item["slot"] for item in after["directions"] if item["active"]) == branch["slot"]
        record("replacement_confirmed", current_preserved=playing, history_preserved=history,
               removed=old_future, replacement=replacement, saved_and_native_and_page=desired)
        print(f"Suffix confirmed; waiting for natural playback of {replacement}", flush=True)

        def replacement_playing(current):
            player = (current.get("browser") or {}).get("player") or {}
            ids = [track["video_id"] for track in current.get("tracks", [])]
            age = current.get("browser_age")
            assert current.get("playlist_id") == before["playlist_id"]
            assert ids[:len(desired)] == desired, "The confirmed playlist prefix changed"
            return (player.get("video_id") == replacement and not player.get("paused")
                    and player.get("position", 0) >= 2 and age is not None and age < 8)

        remaining = player_before["duration"] - player_before["position"]
        final = wait_for(replacement_playing, "actual playback of the replacement", min(remaining + 90, 900))
        record("replacement_played", video_id=replacement, player=final["browser"]["player"],
               native=final["browser"]["native"]["videoIds"],
               saved=[track["video_id"] for track in final["tracks"]])
        trace["passed"] = True
    finally:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(trace, indent=2) + "\n")
        print(f"Live Radio trace: {trace_path}", flush=True)
