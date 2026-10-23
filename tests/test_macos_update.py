import asyncio
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.macos_update import MacOSUpdater, launch_arguments
from yt_playlist.web.jobs import SyncJobs


def test_update_relaunch_restores_address_without_opening_another_tab():
    resume = {"argv": ["--port", "9123", "--host", "localhost"], "time": 1000}
    args = launch_arguments([], resume, now=1010)
    assert "--open" not in args
    assert "--wait-for-port" in args
    assert args[args.index("--port") + 1] == "9123"
    assert args[args.index("--host") + 1] == "localhost"
    assert "--exit-on-idle" in args


@pytest.mark.parametrize("resume", [None, {}, {"time": 999}, {"argv": None, "time": 999},
                                    {"argv": [1], "time": 999},
                                    {"argv": ["--port", "9123"], "time": "bad"},
                                    {"argv": ["--port", "9123"], "time": 0},
                                    {"argv": ["--port", "9123"], "time": 2000}])
def test_ordinary_launch_opens_browser_and_ignores_invalid_or_stale_handoff(resume):
    args = launch_arguments([], resume, now=1000)
    assert "--open" in args and "--exit-on-idle" in args
    assert "--wait-for-port" not in args


def test_explicit_arguments_override_handoff_and_smoke_launch_can_suppress_browser():
    args = launch_arguments(["--port", "9000"], {"argv": ["--port", "9123"], "time": 1000}, now=1010)
    assert args.count("--port") == 1 and args[args.index("--port") + 1] == "9000"
    assert "--open" not in launch_arguments([], no_open=True)


def test_browser_queues_one_native_update_and_allows_retry_after_cancel_or_failure():
    updater = MacOSUpdater()
    async def run():
        assert (await updater.status())["phase"] == "available"
        assert (await updater.install())["phase"] == "installing"
        await updater.install()  # another tab cannot start a concurrent native update cycle
        assert updater.commands.get_nowait() == "check"
        assert updater.commands.empty()
        assert updater.busy
        updater.set_state("error", "Download failed")
        assert not updater.busy
        await updater.install()
        assert updater.commands.get_nowait() == "check"
        updater.set_state("available")  # cancel / remind me later
        assert not updater.busy
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["available", "installing"])
@pytest.mark.parametrize("busy", [None, "sync", "import", "job", "activity"])
def test_native_quit_waits_for_work_then_stops_server_and_blocks_new_work(busy, phase):
    updater = MacOSUpdater()
    updater.set_state(phase)
    ctx = SimpleNamespace(updater=updater, work=Activity(), sync_lock=threading.Lock(),
                          spotify_import_lock=threading.Lock(), jobs=SyncJobs())
    server = SimpleNamespace(config=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx))),
                             should_exit=False)
    updater.attach(server)
    if busy == "job":
        job = ctx.jobs.create()
    elif busy == "activity":
        release = ctx.work.admit("playlist save")
    elif busy is not None:
        lock = ctx.sync_lock if busy == "sync" else ctx.spotify_import_lock
        lock.acquire()
    updater.request_quit()
    if busy is not None:
        updater.poll_quit()
        assert updater.busy
        assert not server.should_exit
        assert not ctx.work.accepting
        with pytest.raises(WorkClosing):
            ctx.work.admit("new mutation")
        assert "Waiting" in updater.snapshot()["message"]
        if busy == "job":
            job.done = True
        elif busy == "activity":
            release()
        else:
            lock.release()
    updater.poll_quit()
    assert server.should_exit
    assert updater.busy
    assert not ctx.work.accepting
    assert ctx.sync_lock.locked() and ctx.spotify_import_lock.locked()
    assert updater.phase == "restarting"
    updater.set_state("available")  # an outstanding Sparkle callback must not undo shutdown
    updater.poll_quit()  # repeated Cocoa timer calls must not try to acquire the held locks again
    assert updater.phase == "restarting"


def test_native_quit_during_server_startup_waits_for_server_attachment():
    updater = MacOSUpdater()
    updater.request_quit()
    updater.poll_quit()
    assert updater.busy
