"""Release checking and desktop update commands with explicit collaborators."""
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.update_rules import CHECK_INTERVAL_S, check_due, update_offer


class UpdateChecks:
    def __init__(self, state, settings, latest_release, *, current_version, install_kind):
        self.state = state
        self.settings = settings
        self.latest_release = latest_release
        self.current_version = current_version
        self.install_kind = install_kind

    def schedule_first(self, now):
        if self.state.release_check() == (None, None, None):
            self.state.schedule_release_check(now + CHECK_INTERVAL_S)

    def check(self, now, *, interval_s=CHECK_INTERVAL_S):
        checked, deadline, _ = self.state.release_check()
        if not check_due(checked, deadline, now, interval_s):
            return
        try:
            latest = self.latest_release()
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            latest = None
        self.state.record_release_check(now, latest)

    def tick(self, now):
        self.schedule_first(now)
        self.check(now)

    def offer(self):
        kind = self.install_kind()
        if kind == 'dev':
            return None
        return update_offer(self.current_version(), self.state.release_check()[2],
                            self.settings.get_setting('backend_update_dismissed_version'), kind)


class UpdateCommands:
    def __init__(self, updater, pending):
        self.updater = updater
        self.pending = pending

    @property
    def available(self):
        return self.updater is not None

    async def status(self):
        return self.pending.snapshot(await self.updater.status())

    async def install(self):
        if self.pending.phase is not None:
            return self.pending.snapshot(self.updater.snapshot()), 202
        self.pending.clear_message()
        return await self.updater.install(), 200

    def restart(self, host, port):
        if self.pending.phase is None and self.updater.phase != 'restarting':
            if self.updater.phase != 'ready':
                raise ValueError('Install the update before restarting.')
            if host not in ('127.0.0.1', 'localhost') or not 0 < port < 65536:
                raise ValueError('Restart needs the local app address.')
            self.pending.request(host, port)
        return self.pending.snapshot(self.updater.snapshot()), 202

    async def cancel(self):
        cancelled = await self.pending.cancel()
        return self.pending.snapshot(self.updater.snapshot()), 200 if cancelled else 409
