"""Synchronize authentication notices with the account owner's confirmed state."""
from yt_playlist.core.account_auth_state import KEY


class AuthSessions:
    def __init__(self, accounts, current, logger):
        self.accounts, self.current, self.logger = accounts, current, logger

    def load(self):
        try:
            self.current.update(self.accounts.auth_expirations())
        except (ValueError, TypeError):
            self.logger.warning('ignoring unreadable %s setting', KEY)

    def _persist(self, **options):
        result = self.accounts.update_auth_expiration(**options)
        if result is None:
            return
        current, invalid = result
        if invalid:
            self.logger.warning('ignoring unreadable %s setting', KEY)
        self.current.clear()
        self.current.update(current)

    def flag(self, account_id, label):
        self.current[account_id] = label or str(account_id)
        self._persist(account_id=account_id, label=self.current[account_id])

    def clear(self, account_id):
        self.current.pop(account_id, None)
        self._persist(account_id=account_id)

    def clear_all(self):
        self.current.clear()
        # A confirmation after repair must clear stale persistence even when
        # the cache was already cleared while writes were unavailable.
        self._persist(clear_all=True)
