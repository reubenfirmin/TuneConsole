"""Release checking and desktop update commands with explicit collaborators."""
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.update_rules import CHECK_INTERVAL_S, check_due, update_offer


class UpdateChecks:
    def __init__(self, settings, latest_release, *, current_version, install_kind):
        self.settings = settings
        self.latest_release = latest_release
        self.current_version = current_version
        self.install_kind = install_kind

    def schedule_first(self, now):
        if all(self.settings.get_setting(key) is None for key in (
                'latest_version_checked_at', 'latest_version_seen', 'latest_version_check_after')):
            self.settings.set_setting('latest_version_check_after', str(now + CHECK_INTERVAL_S))

    def check(self, now, *, interval_s=CHECK_INTERVAL_S):
        if not check_due(self.settings.get_setting('latest_version_checked_at'),
                         self.settings.get_setting('latest_version_check_after'), now, interval_s):
            return
        try:
            latest = self.latest_release()
        except (WorkClosing, WorkBudgetExceeded):
            raise
        except Exception:
            latest = None
        self.settings.set_setting('latest_version_checked_at', str(now))
        if latest:
            self.settings.set_setting('latest_version_seen', latest)

    def tick(self, now):
        self.schedule_first(now)
        self.check(now)

    def offer(self):
        kind = self.install_kind()
        if kind == 'dev':
            return None
        return update_offer(self.current_version(), self.settings.get_setting('latest_version_seen'),
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
