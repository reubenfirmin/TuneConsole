"""Select remote accounts explicitly; provider iteration order is never a write policy."""


def client_for(store, clients, *, account_id=None, playlist_id=None):
    if playlist_id is not None:
        playlist = store.playlists.get_playlist(playlist_id)
        if playlist is None:
            raise ValueError('Playlist no longer exists')
        if account_id is not None and account_id!=playlist.account_id:
            raise ValueError('Playlist belongs to another account')
        account_id = playlist.account_id
    if account_id is None:
        try:
            account_id = store.accounts.get_master_account().id
        except ValueError as exc:
            raise ValueError('Choose a main account in Setup before continuing.') from exc
    active = {i.id for i in store.accounts.get_accounts()}
    client = (clients or {}).get(account_id)
    if account_id not in active or client is None:
        raise ValueError('The required account is not connected')
    return account_id,client


def main_client(store, clients):
    """For optional personal reads: unavailable main means unavailable data."""
    try:
        return client_for(store,clients)[1]
    except ValueError:
        return None


def catalog_client(clients):
    """Public metadata reads may use any connected account; never use this for writes or uploads."""
    clients = clients or {}
    return clients[min(clients)] if clients else None
