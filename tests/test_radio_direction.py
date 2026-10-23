"""Direction changes, tuning, native transitions and stream decay."""
import json

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.routes import radio_session as radio_routes
from tests.radio_session_support import poc, play

def test_latest_direction_choice_updates_only_future_and_waits_for_native_ack(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v2")
    bridge = c.ctx.bridge
    bridge.auto_adopt = False
    bridge.requests.clear()
    state = c.status()
    first, latest = state["directions"][1:3]
    for choice in (first, latest):
        selected = c.select(choice["id"], state["menu_id"])
        assert selected["ok"] and selected["updating"]
        assert selected["pending_slot"] == choice["slot"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert remote.adds == remote.removes == []
    assert bridge.requests == []

    c.advance(c.session)
    desired = ["v1", "v2", latest["tracks"][0]["video_id"]]
    assert remote.ids == desired
    assert remote.adds == [[desired[-1]]]
    assert remote.removes == [["v3"]]
    assert c.status()["updating"]
    assert c.session.directions.active == 0
    assert all(frame["videoIds"] == desired for frame in bridge.requests if frame["refresh"])
    assert any(frame["refresh"] for frame in bridge.requests)
    bridge.native_ids = list(desired)
    c.advance(c.session)
    assert not c.status()["updating"]
    assert c.session.directions.active == latest["slot"]
    assert len(frames) == 1
    assert c.status()["history_count"] == 2
    assert bridge.now_playing["video_id"] == "v2"
    assert bridge.now_playing["position"] == 30


@pytest.mark.parametrize("failure", ["rejected", "lost_response"])
def test_direction_switch_skips_an_unavailable_track_and_preserves_played_history(poc, failure):
    c, remote, *_ = poc
    clock = [1234.]
    c.ctx.now_fn = lambda: clock[0]
    c.start()
    play(c, "v2")
    state = c.status()
    branch = state["directions"][2]
    unavailable = branch["tracks"][0]["video_id"]
    following = branch["tracks"][1]["video_id"]
    add = remote.add_playlist_items
    def fail_one(playlist, ids):
        if ids == [unavailable]:
            remote.adds.append(list(ids))
            if failure == "lost_response":
                raise RuntimeError("Response lost")
            return {"status": "STATUS_FAILED"}
        return add(playlist, ids)
    remote.add_playlist_items = fail_one
    assert c.select(branch["id"], state["menu_id"])["ok"]
    c.advance(c.session)
    if failure == "lost_response":
        assert c.status()["updating"]
        clock[0] += 31
        c.advance(c.session)
    assert remote.ids == ["v1", "v2", following]
    assert remote.adds == [[unavailable], [following]]
    assert remote.removes == [["v3"]]
    assert c.session.replacement is None
    assert c.session.directions.active == 2
    assert unavailable in c.session.rejected
    assert c.session.directions.feedback == {}
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


def test_tuning_preserves_history_and_waits_for_native_future_ack(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v2")
    s, bridge = c.session, c.ctx.bridge
    bridge.auto_adopt = False
    history = list(s.heard)
    choices = dict(s.choices)
    controls = {"comfort": .1, "theme": .9, "energy": .2}
    result = c.tune(controls)
    assert result["ok"] and result["updating"]
    assert s.candidates is s.directions.candidates
    assert result["tuning"] == controls
    assert [b["decay_remaining"] for b in result["directions"] if not b["active"]] == [5, 4, 3]
    assert s.heard == history and s.choices == choices
    assert remote.ids == ["v1", "v2", "v3"]
    assert remote.adds == remote.removes == []
    requested = s.requested_direction["tracks"][0]["video_id"]
    c.advance(s)
    assert remote.ids == ["v1", "v2", requested]
    assert c.status()["updating"]
    assert bridge.now_playing["video_id"] == "v2"
    bridge.native_ids = list(remote.ids)
    c.advance(s)
    assert not c.status()["updating"]
    assert s.heard == history
    assert requested not in s.choices  # Slider changes aren't explicit song choices.
    assert len(frames) == 1
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    assert saved["tuning"] == controls
    assert len(set(saved["directions"]["decay"].values())) == 3


@pytest.mark.parametrize("observe_before_publish", [False, True])
@pytest.mark.parametrize("retune", [False, True])
def test_native_transition_during_tail_removal_stays_in_history(poc, monkeypatch, observe_before_publish, retune):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v1")
    if retune:
        c.tune({"comfort": .1, "theme": .9, "energy": .2})
    else:
        branch = c.status()["directions"][1]
        c.select(branch["id"], c.status()["menu_id"])
    target = c.session.requested_direction["tracks"][0]["video_id"]
    remove = remote.remove_playlist_items

    def transition(playlist, tracks):
        assert tracks[0]["videoId"] == "v2"
        c.ctx.bridge.now_playing["video_id"] = "v2"
        if observe_before_publish:
            c.listening.observe(c.session)
        return remove(playlist, tracks)

    monkeypatch.setattr(remote, "remove_playlist_items", transition)
    c.advance(c.session)
    state = c.status()
    assert remote.ids == ["v1", "v3", target]
    assert [t["video_id"] for t in state["tracks"]] == remote.ids
    assert [t["video_id"] for t in state["history"]] == ["v1", "v2"]
    assert state["history_count"] == 2
    assert state["position"] == -1 and c.session.furthest == 0
    assert state["browser"]["player"]["video_id"] == "v2"
    assert "v2" in c.session.directions.retired
    assert all(t["video_id"] != "v2" for b in state["directions"] for t in b["tracks"])
    assert PlaylistRadio(c.ctx).bootstrap()["state"]["history"] == state["history"]

    # The history endpoint must load metadata by playback identity, even though
    # v2 no longer has membership in the saved playlist.
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        html = client.get("/radio/playlist").text
    assert 'data-vid="v2"' in html and "Song 2" in html
    assert 'data-vid="v3"' not in html
    assert f'data-vid="{target}"' not in html

    play(c, "v3")
    state = c.status()
    assert not state["updating"]
    assert state["browser"]["playback_synced"]
    assert [t["video_id"] for t in state["history"]] == ["v1", "v2", "v3"]
    assert state["ahead"] == 1
    assert len(frames) == 1


def test_tuning_honors_a_pending_explicit_stream_selection(poc):
    c, *_ = poc
    c.start()
    play(c, "v2")
    branch = c.status()["directions"][2]
    c.select(branch["id"], c.status()["menu_id"])
    choice = dict(c.session.requested_direction["choice"])
    result = c.tune({"comfort": .1, "theme": .9, "energy": .2})
    assert result["ok"] and result["pending_slot"] == 2
    assert [b["decay_remaining"] for b in result["directions"] if not b["active"]] == [5, 4, 3]
    assert c.session.requested_direction["choice"] == choice
    c.advance(c.session)
    assert c.session.directions.active == 2
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


@pytest.mark.parametrize("retuned", [False, True])
def test_bootstrap_preserves_staggered_decay_progress(poc, retuned):
    c, *_ = poc
    c.start()
    play(c, "v2")
    if retuned:
        c.tune({"comfort": .1, "theme": .9, "energy": .2})
    c._persist(c.session)
    current = c.status()["directions"]
    restored = PlaylistRadio(c.ctx).bootstrap()["state"]["directions"]
    assert [(b["decay_remaining"], b["decay_total"]) for b in restored] == [
        (b["decay_remaining"], b["decay_total"]) for b in current]
    assert [b["decay_remaining"] for b in restored if not b["active"]] == [5, 4, 3]


