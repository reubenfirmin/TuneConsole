"""Select remote accounts explicitly; provider iteration order is never a write policy."""
from yt_playlist.core.action_errors import ActionInputError


def client_for(store, clients, *, account_id=None, playlist_id=None):
    if playlist_id is not None:
        playlist = store.playlists.get_playlist(playlist_id)
        if playlist is None:
            raise ActionInputError('playlist_missing')
        if account_id is not None and account_id!=playlist.account_id:
            raise ActionInputError('playlist_account_mismatch')
        account_id = playlist.account_id
    accounts = store.accounts.get_accounts()
    if account_id is None:
        main = next((account for account in accounts if account.is_master), None)
        if main is None:
            raise ActionInputError('main_account_required')
        account_id = main.id
    active = {i.id for i in accounts}
    client = (clients or {}).get(account_id)
    if account_id not in active or client is None:
        raise ActionInputError('account_disconnected')
    return account_id,client


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
