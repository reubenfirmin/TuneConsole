"""Select remote accounts explicitly; provider iteration order is never a write policy."""


def client_for(store, clients, *, identity_id=None, playlist_id=None):
    if playlist_id is not None:
        playlist = store.playlists.get_playlist(playlist_id)
        if playlist is None:
            raise ValueError('Playlist no longer exists')
        if identity_id is not None and identity_id!=playlist.identity_id:
            raise ValueError('Playlist belongs to another account')
        identity_id = playlist.identity_id
    if identity_id is None:
        try:
            identity_id = store.identities.get_master_identity().id
        except ValueError as exc:
            raise ValueError('Choose a main account in Setup before continuing.') from exc
    active = {i.id for i in store.identities.get_identities()}
    client = (clients or {}).get(identity_id)
    if identity_id not in active or client is None:
        raise ValueError('The required account is not connected')
    return identity_id,client


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
