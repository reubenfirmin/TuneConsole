"""Select remote accounts explicitly; provider iteration order is never a write policy."""
from yt_playlist.core.action_errors import ActionInputError


def select_account_id(*, playlist=None, accounts, account_id=None):
    """Pure owner-selection rule: which account id a client must resolve to.

    A playlist's recorded owner always wins over an unspecified default, and an
    explicit mismatch is rejected outright. A missing recorded owner is never
    silently replaced by the current main account.
    """
    if playlist is not None:
        if account_id is not None and account_id != playlist.account_id:
            raise ActionInputError('playlist_account_mismatch')
        return playlist.account_id
    if account_id is not None:
        return account_id
    main = next((account for account in accounts if account.is_master), None)
    if main is None:
        raise ActionInputError('main_account_required')
    return main.id


def select_remaining_master(accounts):
    """Pure: which remaining account becomes master after an identity is removed."""
    return next((account for account in accounts if account.is_master), accounts[0])


def client_for(store, clients, *, account_id=None, playlist_id=None):
    return AccountAccess(store.accounts, store.playlists, clients).resolve(
        account_id=account_id, playlist_id=playlist_id)


class AccountAccess:
    """Resolve the exact write capability once from account and playlist facts."""
    def __init__(self, accounts, playlists, clients):
        self.accounts, self.playlists, self.clients = accounts, playlists, clients or {}

    def resolve(self, *, account_id=None, playlist_id=None):
        playlist = None
        if playlist_id is not None:
            playlist = self.playlists.get_playlist(playlist_id)
            if playlist is None:
                raise ActionInputError('playlist_missing')
        accounts = self.accounts.get_accounts()
        account_id = select_account_id(playlist=playlist, accounts=accounts, account_id=account_id)
        clients = self.clients() if callable(self.clients) else self.clients
        client = clients.get(account_id)
        if account_id not in {account.id for account in accounts} or client is None:
            raise ActionInputError('account_disconnected')
        return account_id, client


def main_client(store, clients):
    """For optional personal reads: unavailable main means unavailable data."""
    try:
        return client_for(store,clients)[1]
    except ActionInputError:
        return None


def catalog_client(clients):
    """Public metadata reads may use any connected account; never use this for writes or uploads."""
    clients = clients or {}
    return clients[min(clients)] if clients else None
