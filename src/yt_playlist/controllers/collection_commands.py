"""Account-scoped collection commands, including confirmed-action effects."""
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.album_writes import saved_album, confirm_saved


def removal_owner(owners, account_id=None):
    if account_id is None:
        if len(owners) != 1 or owners[0]['account_id'] is None:
            raise ActionInputError('album_owner_required')
        account_id = owners[0]['account_id']
    if account_id not in {owner['account_id'] for owner in owners}:
        raise ActionInputError('album_owner_mismatch')
    return account_id


class CollectionCommands:
    def __init__(self, collection, access, *, now, respond, refresh):
        self.collection, self.access = collection, access
        self.now, self.respond, self.refresh = now, respond, refresh

    def albums(self, view):
        if view == 'saved':
            return dict(view=view, saved=self.collection.get_saved_albums(),
                        saved_owners=self.collection.saved_album_owners())
        return dict(view='playlists', albums=self.collection.collection_albums())

    def artists(self):
        return self.collection.collection_artists()

    def save(self, browse_id, opportunity=None):
        return self._change(browse_id, saved=True, opportunity=opportunity)

    def remove(self, browse_id, account_id=None):
        owners = self.collection.saved_album_owners(browse_id).get(browse_id, [])
        owner = removal_owner(owners, account_id)
        return self._change(browse_id, saved=False, account_id=owner)

    def _change(self, browse_id, *, saved, account_id=None, opportunity=None):
        account_id, client = self.access.resolve(account_id=account_id)
        album = client.get_album(browse_id)
        confirm_saved(client, album, saved=saved)
        if saved:
            self.collection.add_saved_album(saved_album(browse_id, album), account_id=account_id)
            if opportunity:
                self.respond(opportunity, 'save', self.now(), option_id=browse_id)
            self.refresh()
        else:
            self.collection.remove_saved_album(browse_id, account_id=account_id)
        return dict(account_id=account_id, browse_id=browse_id, saved=saved)
