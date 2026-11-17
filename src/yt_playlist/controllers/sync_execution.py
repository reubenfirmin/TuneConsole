"""One synchronization attempt, independent of HTTP jobs and progress framing."""
from dataclasses import dataclass
from yt_playlist.core.activity import WorkClosing

from yt_playlist.library.sync_outcomes import resource
from yt_playlist.util.retry import error_summary


@dataclass(frozen=True)
class SyncOutcome:
    result: dict
    outcome: str
    error: str | None = None


class SyncExecution:
    def __init__(self, *, begin, synchronize, clients, accounts, finish, now, wake):
        self.begin, self.synchronize, self.clients = begin, synchronize, clients
        self.accounts, self.finish, self.now, self.wake = accounts, finish, now, wake

    def run(self, progress, *, automatic=False, retry_now=False, max_age_s=86400):
        now = self.now()
        try:
            self.begin(now)
            result = self.synchronize(self.clients() or {}, now, on_progress=progress,
                automatic=automatic, retry_now=retry_now, max_age_s=max_age_s)
            outcome = result['status']
            error = (f'Sync {outcome}; some library resources could not be refreshed.'
                     if outcome != 'complete' else None)
            return SyncOutcome(result, outcome, error)
        except WorkClosing:
            raise
        except Exception as exc:
            detail = error_summary(exc)
            progress({'type': 'err', 'text': f'sync failed: {detail}'})
            accounts = {str(identity.id): {'label': identity.label, 'status': 'failed', 'resources': {
                name: resource('failed', detail) for name in
                ('playlist_listing', 'playlists', 'ratings', 'history', 'saved_albums')}}
                for identity in self.accounts()}
            result = self.finish({'status': 'failed', 'accounts': accounts, 'resources': {
                name: resource('failed', detail) for name in ('saved_album_mirror', 'album_tracks')}}, now)
            return SyncOutcome(result, 'failed', detail)
        finally:
            self.wake()
