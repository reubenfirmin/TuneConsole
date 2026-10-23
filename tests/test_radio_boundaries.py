"""Lifecycle and restart boundaries across the independent radio collaborators."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_radio_session import poc, play
from yt_playlist.web.radio_persistence import migrate
from yt_playlist.web.radio_edits import AppendOperation
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending, RadioPlaylistIO
from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail, TailView
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_state import Phase, Session


def test_lifecycle_rejects_restarting_a_stopped_session():
    s = Session(None, None, None, "Radio")
    with pytest.raises(ValueError, match="Illegal radio transition"):
        s.move(Phase.APPENDING)
    s.move(Phase.AWAITING_PLAYBACK)
    s.playback_confirmed = True
    s.settle()
    assert s.phase == Phase.PLAYING
    s.request_stop()
    s.move(Phase.STOPPED)
    s.edits.begin(AppendOperation({"video_id": "late-write"}))
    s.settle()
    assert s.phase == Phase.STOPPED
    with pytest.raises(ValueError, match="Illegal radio transition"):
        s.move(Phase.APPENDING)


@pytest.mark.parametrize(("changes", "phase"), [
    ({}, Phase.PLAYING), ({"initial_tracks": [{}]}, Phase.PREPARING),
    ({"pending": {"video_id": "pending"}}, Phase.APPENDING),
    ({"replacement": {"slot": 1}}, Phase.REPLACING),
    ({"pending": {}, "halted": True}, Phase.CONFLICT),
    ({"stopped": True, "halted": True}, Phase.STOPPED),
])
def test_legacy_flags_have_explicit_migration_precedence(changes, phase):
    old = {"version": 1, **changes}
    assert migrate(old)["phase"] == phase
    assert old["version"] == 1
    with pytest.raises(ValueError, match="Unsupported"):
        migrate({"version": 9000})


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("replacement", [False, True])
def test_restart_checks_an_uncertain_write_before_sending_another(poc, version, replacement):
    c, remote, *_ = poc
    c.start()
    assert c.session.phase == Phase.AWAITING_PLAYBACK
    play(c, "v1" if replacement else "v3")
    if not replacement:
        # v3's first observation already appended v4. Consume it to need v5.
        video = remote.ids[-1]
        c.ctx.bridge.now_playing["video_id"] = video
        c._browser(c.session)
    else:
        branch = c.status()["directions"][1]
        c.select(branch["id"], c.status()["menu_id"])
    phase = Phase.REPLACING if replacement else Phase.APPENDING
    remote.after_add = lambda: setattr(remote, "fail_read", True)
    before = len(remote.adds)
    with pytest.raises(RuntimeError, match="Read response lost"):
        c.advance(c.session)
    assert c.session.phase == phase
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    assert saved["phase"] == phase
    saved["version"] = version
    if version == 1:
        saved.pop("phase")
    c.ctx.store.settings.set_setting("radio_current_session", json.dumps(saved))
    remote.after_add = lambda: None
    restored = PlaylistRadio(c.ctx)
    restored.join()
    assert restored.session.phase == phase
    assert len(remote.adds) == before + 1
    restored._browser(restored.session)
    restored.advance(restored.session)
    assert restored.session.phase == Phase.PLAYING
    assert len(remote.adds) == before + 1
    assert [t["video_id"] for t in restored.session.tracks] == remote.ids
    assert restored.session.tracks[0]["video_id"] == "v1"
    restored.stop()


def test_stop_during_a_tail_write_cannot_reactivate_the_session(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    branch = c.status()["directions"][1]
    c.select(branch["id"], c.status()["menu_id"])
    remote.after_add = c.stop
    c.advance(c.session)
    assert c.session.phase == Phase.STOPPING
    assert c.session.stop.is_set()
    assert not c.status()["active"]
    adds, removes = list(remote.adds), list(remote.removes)
    c.advance(c.session)
    assert (remote.adds, remote.removes) == (adds, removes)
    checkpoint = c.persistence.load()
    assert checkpoint["stopped"] and checkpoint["phase"] == Phase.STOPPING


@pytest.mark.parametrize("version", [1, 2])
def test_restart_does_not_adopt_a_session_the_user_stopped(poc, version):
    c, remote, frames, workers, _ = poc
    c.start()
    play(c, "v1")
    c.stop()
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    saved["version"] = version
    if version == 1:
        saved.pop("phase")
    c.ctx.store.settings.set_setting("radio_current_session", json.dumps(saved))
    before = (list(remote.adds), list(remote.removes), list(frames), len(workers))
    restored = PlaylistRadio(c.ctx)
    assert not restored.bootstrap()["state"]["active"]
    assert not restored.join()["active"]
    assert restored.session is None
    assert (remote.adds, remote.removes, frames, len(workers)) == before
    assert restored.persistence.load()["stopped"]


def test_conflict_recovers_only_after_owned_contents_return(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    owned = list(remote.ids)
    remote.ids.append("unrelated-edit")
    c.session.move(Phase.CONFLICT)
    assert not c._recover(c.session)
    assert c.session.phase == Phase.CONFLICT and remote.ids[-1] == "unrelated-edit"
    remote.ids = owned
    assert c._recover(c.session)
    assert c.session.phase == Phase.PLAYING
    assert not remote.adds and not remote.removes


def test_tail_preserves_history_and_waits_for_complete_owned_readback():
    tracks = [{"video_id": v} for v in ("past", "playing", "old", "new")]
    known = {t["video_id"]: t for t in tracks}
    tail = RadioPlaylistTail(tracks[:3], [tracks[-1]], 1)
    view = TailView(tuple(tracks[:3]), 1, 1, frozenset(), known)
    remote = [{"videoId": t["video_id"], "setVideoId": t["video_id"]} for t in tracks[:3]]
    decision = tail.plan(remote, view, {"video_id": "playing", "duration": 300, "position": 30})
    assert decision.edit.kind == "add"
    assert decision.edit.after == ("past", "playing", "old", "new")
    tail.begin(decision.edit, 10)
    assert tail.accept(remote, view)
    assert tail.pending(tuple(row["videoId"] for row in remote), 11).rejected is None
    assert tail.pending(tuple(row["videoId"] for row in remote), 40).rejected == tracks[-1]
    with pytest.raises(PlaylistChanged):
        tail.accept([{**remote[0], "videoId": "unrelated"}, *remote[1:]], view)


def test_playlist_io_never_publishes_a_partial_read(store):
    io = RadioPlaylistIO(store.playlists)
    client = SimpleNamespace(get_playlist=lambda *_args, **_kw: {
        "tracks": [{"videoId": "one"}], "trackCount": 2})
    with pytest.raises(PlaylistPending):
        io.read(client, "playlist")
    # Only initial exact accepted contents may override a lagging header count.
    assert len(io.read(client, "playlist", expected=["one"])) == 1
    with pytest.raises(PlaylistPending):
        io.read(client, "playlist", expected=["one", "two"])


def test_tail_has_no_service_callbacks_or_session_transport_access():
    path = Path(__file__).resolve().parents[1] / "src/yt_playlist/web/radio_playlist_tail.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"ctx", "client", "session", "_persist", "_publish", "_browser", "_player"}
