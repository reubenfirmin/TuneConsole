"""Automatic recovery keeps durable evidence and never guesses remote absence."""
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from tests.conftest import _track
from tests.test_saved_album_snapshot import _production_client
from tests.test_undo_recovery import RecoveryClient
from yt_playlist.core.activity import Activity
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_backups, playlist_deletion, playlist_gc_retry, playlist_generated
from yt_playlist.library.playlist_generated import gc_generated_playlists
from yt_playlist.util.action_kinds import DELETE_PLAYLIST, GC_GENERATED

NOW = 20 * 86400


class Client(RecoveryClient):
    def __init__(self):
        super().__init__({"SOURCE": ["a", "b"]})
        self.reads = []
        self.writes = []
        self.read_failure = None

    def get_playlist(self, playlistId, limit=None):
        self.reads.append((playlistId, limit))
        if self.read_failure is not None:
            if isinstance(self.read_failure, Exception):
                raise self.read_failure
            return self.read_failure
        return super().get_playlist(playlistId, limit)

    def delete_playlist(self, playlistId):
        self.writes.append(playlistId)
        return super().delete_playlist(playlistId)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    monkeypatch.setattr(playlist_backups, "with_retry", lambda fn, **kwargs: fn())
    store = Store(tmp_path / "gc.db")
    store.init_schema()
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(iid, "SOURCE", "Generated", 2, "", 1)
    tids = [store.tracks.upsert_track(v, v, "Artist", None, None, 1) for v in ("a", "b")]
    store.playlists.set_playlist_tracks(pid, tids)
    store.playlists.mark_generated_playlist("SOURCE")
    client = Client()
    result = SimpleNamespace(store=store, iid=iid, pid=pid, client=client,
                             path=tmp_path / "gc.db", tmp=tmp_path)
    yield result
    result.store.conn.close()


def collect(case, now=NOW, **kwargs):
    return gc_generated_playlists(case.store, {case.iid: case.client}, now, grace_days=1, **kwargs)


def failed_attempt(case, kind=GC_GENERATED):
    pl = case.store.playlists.get_playlist(case.pid)
    backup = playlist_backups.backup_playlist(case.store, pl.id, NOW - 1, case.client)
    case.client.reject_delete = True
    with pytest.raises(ValueError, match="rejected"):
        playlist_deletion.delete_backed_up(case.store, pl, case.client, backup, NOW - 1, kind=kind)
    case.client.reject_delete = False
    case.client.reads.clear()
    case.client.writes.clear()
    return case.store.actions.get_actions()[0].id, Path(backup), Path(backup).read_bytes()


def reopen(case):
    case.store.conn.close()
    case.store = Store(case.path)
    case.store.init_schema()


@pytest.mark.parametrize("response", [
    YTMusicServerError("Server returned HTTP 404: Not Found."), {"error": {"code": 404}},
])
def test_saved_failure_confirmed_absent_completes_original_without_another_delete(case, response):
    aid, backup, original = failed_attempt(case)
    reopen(case)
    case.client.read_failure = response

    result = collect(case)

    assert [row["backup"] for row in result] == [str(backup)]
    assert case.store.actions.get_action(aid).status == "executed"
    assert case.store.playlists.get_playlist(case.pid) is None
    assert len(case.store.actions.get_actions()) == 1
    assert case.client.writes == []
    assert backup.read_bytes() == original
    assert playlist_gc_retry.states(case.store) == {}


def test_confirmed_survivor_requires_fresh_complete_backup_and_committed_intent(case, monkeypatch):
    old_aid, old_backup, original = failed_attempt(case)
    case.client._tracks["SOURCE"].append(_track("new", "New", "Artist", set_video_id="new-handle"))
    delete = case.client.delete_playlist

    def checked_delete(ytm):
        newest = case.store.actions.get_actions()[0]
        assert newest.id != old_aid and newest.status == "delete_pending"
        entry, = json.loads(newest.undo_json)["deletions"]
        saved = json.loads(Path(entry["backup"]).read_text())
        assert [row["video_id"] for row in saved["tracks"]] == ["a", "b", "new"]
        assert entry["backup"] != str(old_backup)
        return delete(ytm)

    monkeypatch.setattr(case.client, "delete_playlist", checked_delete)
    result = collect(case)

    assert len(result) == 1 and case.client.writes == ["SOURCE"]
    assert old_backup.read_bytes() == original
    assert case.store.actions.get_action(old_aid).status == "delete_failed"
    assert case.store.actions.get_actions()[0].status == "executed"


@pytest.mark.parametrize("response", [
    {}, {"tracks": [], "trackCount": 2}, {"tracks": [], "continuation": "next"},
    {"tracks": [], "_readAudit": {"exhausted": False}},
    {"tracks": [], "_readAudit": {"exhausted": True, "omitted": 1}},
    {"error": {"code": 403}}, TimeoutError("Read interrupted"),
    YTMusicServerError("Server returned HTTP 401: Unauthorized."),
    YTMusicServerError("Server returned HTTP 403: Forbidden."),
])
def test_unknown_remote_state_preserves_local_row_backup_and_saved_action(case, response):
    aid, backup, original = failed_attempt(case)
    case.client.read_failure = response

    assert collect(case) == []

    assert case.store.playlists.get_playlist(case.pid) is not None
    assert case.store.actions.get_action(aid).status == "delete_pending"
    assert len(case.store.actions.get_actions()) == 1
    assert case.client.writes == [] and backup.read_bytes() == original
    assert list((case.tmp / "backups").glob("*.json")) == [backup]


@pytest.mark.parametrize("raw", [
    {"responseContext": {}, "trackingParams": "private", "frameworkUpdates": {}},
    {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}},
    {"error": {"code": 403, "message": "Denied"}},
])
def test_production_strict_reader_rejects_realistic_incomplete_and_denied_envelopes(case, raw):
    aid, backup, original = failed_attempt(case)
    production, bridge = _production_client([raw])
    case.client = production

    assert collect(case) == []

    assert len(bridge.calls) == 1
    assert all("browse" in url for url, body in bridge.calls)
    assert case.store.actions.get_action(aid).status == "delete_pending"
    assert case.store.playlists.get_playlist(case.pid) is not None
    assert backup.read_bytes() == original


def test_production_complete_pages_back_up_new_members_before_fresh_delete(case, monkeypatch):
    from tests.core.test_playlist_page_accounting import first, item
    from ytmusicapi.navigation import PLAY_BUTTON

    def playable(video_id):
        # The accounting fixture intentionally has no playable identity. Supply
        # the title endpoint and play control of an actual playable Music row.
        raw = item(video_id)
        row = raw["musicResponsiveListItemRenderer"]
        row.pop("musicItemRendererDisplayPolicy")
        row["flexColumns"][0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0][
            "navigationEndpoint"] = {"watchEndpoint": {"videoId": video_id}}
        button = row
        for field in PLAY_BUTTON:
            button = button.setdefault(field, {})
        button["playNavigationEndpoint"] = {"watchEndpoint": {"videoId": video_id}}
        return raw

    old_aid, backup, original = failed_attempt(case)
    responses = [first([playable("a"), playable("b")], 2),
                 first([playable("a"), playable("b"), playable("new")], 3)]
    for response in responses:
        header = response["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]
        header["content"]["sectionListRenderer"]["contents"][0]["musicResponsiveHeaderRenderer"][
            "buttons"][1]["musicPlayButtonRenderer"]["playNavigationEndpoint"]["watchEndpoint"][
            "playlistId"] = "SOURCE"
    production, bridge = _production_client(responses)
    old_read = case.client.get_playlist

    def read(playlist_id, limit=None):
        if playlist_id in case.client.deleted:
            return old_read(playlist_id, limit)
        return production.get_playlist(playlist_id, limit=limit)

    monkeypatch.setattr(case.client, "get_playlist", read)
    result = collect(case)

    assert len(result) == 1 and case.client.writes == ["SOURCE"]
    assert len(bridge.calls) == 2
    assert backup.read_bytes() == original
    fresh = json.loads(Path(result[0]["backup"]).read_text())
    assert [row["video_id"] for row in fresh["tracks"]] == ["a", "b", "new"]
    assert case.store.actions.get_actions()[0].id != old_aid


def test_backoff_survives_reopen_and_repeated_failure_has_only_one_trace(case, caplog):
    failed_attempt(case)
    case.client.read_failure = TimeoutError("Temporarily unavailable")
    caplog.set_level(logging.WARNING, logger=playlist_gc_retry.__name__)
    assert collect(case) == []
    first, = [r for r in caplog.records if r.name == playlist_gc_retry.__name__]
    assert first.exc_info
    pl = case.store.playlists.get_playlist(case.pid)
    deadline = NOW + 3600
    assert playlist_gc_retry.read(case.store, pl)["next_retry_at"] == deadline
    reopen(case)
    case.client.reads.clear()
    caplog.clear()

    assert collect(case, deadline - 1, retry_only=True) == []
    assert case.client.reads == [] and caplog.records == []
    assert collect(case, deadline, retry_only=True) == []
    repeated, = [r for r in caplog.records if r.name == playlist_gc_retry.__name__]
    assert not repeated.exc_info
    assert "Traceback" not in caplog.text
    assert "attempt=2" in repeated.getMessage()
    assert playlist_gc_retry.read(case.store, pl)["next_retry_at"] == deadline + 7200

    caplog.clear()
    case.client.read_failure = YTMusicServerError("Server returned HTTP 403: Forbidden.")
    collect(case, deadline + 7200, retry_only=True)
    changed, = [r for r in caplog.records if r.name == playlist_gc_retry.__name__]
    assert changed.exc_info
    assert case.client.writes == []


def test_backoff_reaches_but_never_exceeds_one_day(case):
    failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    now = NOW
    for delay in (3600, 7200, 14400, 28800, 57600, 86400, 86400):
        collect(case, now, retry_only=now != NOW)
        state = next(iter(playlist_gc_retry.states(case.store).values()))
        assert state["next_retry_at"] == now + delay
        now += delay
    assert case.client.writes == []


def test_unknown_retry_survives_sync_removal_then_resolves_read_only(case):
    aid, backup, original = failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    collect(case)
    case.store.playlists.remove_playlist(case.pid)
    reopen(case)

    assert collect(case, NOW + 3600, retry_only=True) == []
    assert case.store.actions.get_action(aid).status == "delete_pending"
    assert playlist_gc_retry.states(case.store)
    case.client.read_failure = {"error": {"code": 404}}
    assert len(collect(case, NOW + 3600 + 7200, retry_only=True)) == 1
    assert case.store.actions.get_action(aid).status == "executed"
    assert playlist_gc_retry.states(case.store) == {}
    assert case.client.writes == [] and backup.read_bytes() == original


def test_missing_local_row_confirmed_present_never_authorizes_a_fresh_delete(case):
    failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    collect(case)
    case.store.playlists.remove_playlist(case.pid)
    case.client.read_failure = None

    assert collect(case, NOW + 3600, retry_only=True) == []
    assert case.client.writes == []
    assert playlist_gc_retry.states(case.store) == {}
    assert len(list((case.tmp / "backups").glob("*.json"))) == 1


def test_lost_delete_reply_is_reconciled_at_deadline_without_replaying_write(case):
    case.client.lose_delete = True
    assert collect(case) == []
    aid = case.store.actions.get_actions()[0].id
    reopen(case)
    reads = list(case.client.reads)

    assert collect(case, NOW + 3599, retry_only=True) == []
    assert case.client.reads == reads
    assert len(collect(case, NOW + 3600, retry_only=True)) == 1
    assert case.client.writes == ["SOURCE"]
    assert case.store.actions.get_action(aid).status == "executed"


def test_interrupted_local_completion_resumes_committed_absence_without_remote_read(case, monkeypatch):
    prune = playlist_deletion._prune

    def interrupt(store, action, entry):
        prune(store, action, entry)
        raise SystemExit("Interrupted after local cleanup")

    monkeypatch.setattr(playlist_deletion, "_prune", interrupt)
    with pytest.raises(SystemExit):
        collect(case)
    aid = case.store.actions.get_actions()[0].id
    assert case.store.playlists.get_playlist(case.pid) is None
    reopen(case)
    monkeypatch.setattr(playlist_deletion, "_prune", prune)
    case.client.read_failure = TimeoutError("Disconnected after confirmed deletion")
    reads = list(case.client.reads)

    assert len(collect(case, NOW + 3600, retry_only=True)) == 1
    assert case.client.reads == reads and case.client.writes == ["SOURCE"]
    assert case.store.actions.get_action(aid).status == "executed"


def test_automatic_gc_cannot_retry_a_failed_manual_deletion(case):
    aid, backup, original = failed_attempt(case, kind=DELETE_PLAYLIST)

    assert collect(case) == []

    assert case.client.writes == [] and backup.read_bytes() == original
    assert case.store.actions.get_action(aid).status == "delete_failed"
    assert len(case.store.actions.get_actions()) == 1


@pytest.mark.parametrize("reason", ["adopted", "played", "young"])
def test_retry_rechecks_current_eligibility_before_any_remote_read(case, reason):
    failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    collect(case)
    if reason == "adopted":
        case.store.playlists.adopt_generated_playlist("SOURCE")
    elif reason == "played":
        keys = case.store.playlists.get_playlist_track_keys(case.pid)
        case.store.history.record_history_plays(case.iid, NOW + 1, list(keys), source_timezone="UTC")
    else:
        with case.store.conn:
            case.store.conn.execute("UPDATE playlists SET first_seen=? WHERE id=?", (NOW, case.pid))
    case.client.reads.clear()

    assert collect(case, NOW + 3600, retry_only=True) == []
    assert case.client.reads == [] and case.client.writes == []
    assert playlist_gc_retry.states(case.store) == {}


@pytest.mark.parametrize("source", ["sync", "generated_playback"])
def test_listening_completed_during_admission_prevents_automatic_delete(case, monkeypatch, source):
    original_claim = playlist_generated.claim_deletion_target
    admitted = []

    @contextmanager
    def claim_after_listening(store, identity_id, remote_id):
        with original_claim(store, identity_id, remote_id):
            keys = store.playlists.get_playlist_track_keys(case.pid)
            if source == "sync":
                store.history.record_history_plays(
                    identity_id, NOW + 1, list(keys), source_timezone="UTC")
            else:
                for key, video_id in zip(sorted(keys), ("a", "b")):
                    store.history.record_play_event(identity_id, key, video_id, NOW + 1,
                                                    playlist_ytm_id=remote_id)
            admitted.append((identity_id, remote_id))
            yield

    monkeypatch.setattr(playlist_generated, "claim_deletion_target", claim_after_listening)

    assert collect(case, NOW + 2) == []

    assert admitted == [(case.iid, "SOURCE")]
    assert case.client.reads == [] and case.client.writes == []
    assert case.store.playlists.get_playlist(case.pid) is not None
    assert case.store.actions.get_actions() == []


@pytest.mark.parametrize("reason", ["played", "adopted", "young", "removed"])
def test_eligibility_changed_during_backup_prevents_automatic_delete(case, monkeypatch, reason):
    backup = playlist_generated.backup_playlist
    retained = []

    def backup_then_change(*args, **kwargs):
        path = backup(*args, **kwargs)
        retained.append(Path(path))
        if reason == "played":
            keys = case.store.playlists.get_playlist_track_keys(case.pid)
            case.store.history.record_history_plays(case.iid, NOW, list(keys), source_timezone="UTC")
        elif reason == "adopted":
            case.store.playlists.adopt_generated_playlist("SOURCE")
        elif reason == "young":
            with case.store.conn:
                case.store.conn.execute("UPDATE playlists SET first_seen=? WHERE id=?", (NOW, case.pid))
        else:
            case.store.playlists.remove_playlist(case.pid)
        return path

    monkeypatch.setattr(playlist_generated, "backup_playlist", backup_then_change)

    assert collect(case) == []

    assert case.client.writes == []
    assert len(retained) == 1 and retained[0].is_file()
    assert [row["video_id"] for row in json.loads(retained[0].read_text())["tracks"]] == ["a", "b"]
    assert case.store.actions.get_actions() == []
    assert playlist_gc_retry.states(case.store) == {}


def test_explicit_read_only_recheck_can_resolve_during_automatic_backoff(case):
    aid, backup, original = failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    collect(case)
    case.client.read_failure = {"error": {"code": 404}}

    result = playlist_deletion.reconcile_deletion(case.store, aid, {case.iid: case.client}, NOW + 1)

    assert result.status == "executed"
    assert case.client.writes == [] and backup.read_bytes() == original


def test_failed_target_does_not_block_healthy_playlist_and_retry_only_leaves_new_targets(case):
    failed_attempt(case)
    case.client.read_failure = TimeoutError("Unavailable")
    collect(case)
    healthy = Client()
    other = case.store.identities.upsert_identity("Other", "bridge", "brand", False)
    pid = case.store.playlists.upsert_playlist(other, "SOURCE", "Other generated", 2, "", 1)
    case.store.playlists.set_playlist_provenance("SOURCE", identity_id=other,
                                                origin="tuneconsole", adopted=False)
    clients = {case.iid: case.client, other: healthy}

    assert gc_generated_playlists(case.store, clients, NOW + 1, grace_days=1, retry_only=True) == []
    assert healthy.reads == [] and healthy.writes == []
    result = gc_generated_playlists(case.store, clients, NOW + 2, grace_days=1)
    assert len(result) == 1 and healthy.writes == ["SOURCE"]
    assert case.store.playlists.get_playlist(pid) is None
    assert case.store.playlists.get_playlist(case.pid) is not None
    assert case.client.writes == []
    state, = playlist_gc_retry.states(case.store).values()
    assert state["identity"] == case.iid and state["attempts"] == 1


def test_explicit_manual_retry_keeps_existing_semantics_despite_automatic_backoff(case):
    from yt_playlist.library import executor

    failed_attempt(case, kind=DELETE_PLAYLIST)
    collect(case)
    assert playlist_gc_retry.states(case.store)

    executor.delete_playlist(case.store, case.pid, case.client, NOW + 1)

    assert case.client.writes == ["SOURCE"]
    assert case.store.playlists.get_playlist(case.pid) is None


def test_worker_wakes_for_due_retry_and_keeps_discovery_on_daily_cadence(case, monkeypatch):
    from yt_playlist.rec import discover, rec_worker
    from yt_playlist.library import playlist_generated

    clock, waits, calls, discoveries = [0], [], [], []

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            clock[0] += seconds
            return len(calls) >= 3

        def is_set(self):
            return False

    def collection(store, clients, now, *, retry_only=False):
        calls.append((now, retry_only))
        if len(calls) == 1:
            playlist_gc_retry.begin(store, store.playlists.get_playlist(case.pid), now, None)
        else:
            playlist_gc_retry.clear(store, case.iid, "SOURCE")
        return []

    monkeypatch.setattr(rec_worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(playlist_generated, "gc_generated_playlists", collection)
    monkeypatch.setattr(discover, "gc_discovery", lambda ctx, now: discoveries.append(now) or {})
    worker = rec_worker.RecWorker.__new__(rec_worker.RecWorker)
    worker.ctx = SimpleNamespace(store=case.store, client_provider=lambda: {case.iid: case.client},
                                 now_fn=lambda: NOW + clock[0], logger=logging.getLogger(__name__))
    worker.work, worker._stop = Activity(), Stop()
    worker.gc_initial_s, worker.gc_tick_s = 0, 86400

    worker._gc_loop()

    assert calls == [(NOW, False), (NOW + 3600, True), (NOW + 86400, False)]
    assert discoveries == [NOW, NOW + 86400]
    assert waits == [0, 3600, 82800, 86400]
