"""Journal catalog saves and reconcile ambiguous outcomes without repeating writes."""
import json
from threading import Lock

from yt_playlist.util.action_kinds import SAVE_CATALOG_ENTITY
from yt_playlist.util.music_catalog import ARTIST_ID, VIDEO_ID


class CatalogLibrary:
    def __init__(self, access, actions, *, now, publish, logger):
        self.access, self.actions = access, actions
        self.now, self.publish, self.logger = now, publish, logger
        self._writes = Lock()

    def pending(self, account):
        return {(params['kind'], params['browse_id']): action.id
                for action in self.actions.get_pending_actions(SAVE_CATALOG_ENTITY)
                if (params := json.loads(action.params_json))['account_id'] == account}

    def _observe(self, client, kind, browse):
        if kind == 'artist':
            detail = client.get_artist(browse)
            target = detail.get('channelId')
            if not isinstance(target, str) or not ARTIST_ID.fullmatch(target):
                raise ValueError('Artist subscription identity unavailable')
            saved = detail.get('subscribed')
        elif kind == 'album':
            detail = client.get_album(browse)
            target = detail.get('audioPlaylistId')
            saved = detail.get('likeStatus')
            saved = saved == 'LIKE' if saved in ('LIKE', 'INDIFFERENT', 'DISLIKE') else None
        else:
            target = browse.removeprefix('VL')
            detail = client.get_playlist(target, limit=1)
            # get_playlist does not expose playlist library membership. Only a
            # complete native listing can establish absence, including past page 1.
            rows = client.get_library_playlists(limit=None)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('playlistId') for row in rows):
                raise ValueError('Incomplete playlist library observation')
            saved = any(row['playlistId'] == target for row in rows)
        if not isinstance(target, str) or not VIDEO_ID.fullmatch(target) or type(saved) is not bool:
            raise ValueError('Catalog library membership unavailable')
        return saved, target, detail

    def save(self, kind, browse, *, check=False):
        if kind not in ('artist', 'album', 'playlist') or not VIDEO_ID.fullmatch(browse):
            raise ValueError('Invalid catalog identity')
        if ((kind == 'artist' and not ARTIST_ID.fullmatch(browse))
                or (kind == 'album' and not browse.startswith('MPRE'))):
            raise ValueError('Invalid catalog identity')
        if kind == 'playlist':
            if not VIDEO_ID.fullmatch(browse.removeprefix('VL')):
                raise ValueError('Invalid catalog identity')
            browse = 'VL' + browse.removeprefix('VL')
        account, client = self.access.resolve()
        if not self._writes.acquire(timeout=3):
            raise TimeoutError('Catalog save already in progress')
        try:
            action_id = self.pending(account).get((kind, browse))
            saved, target, detail = self._observe(client, kind, browse)

            def confirm():
                self.publish(kind, browse, account, client, detail)
                if action_id is not None:
                    self.actions.update_action(action_id, 'executed', self.now())
                return {'state': 'saved'}

            if saved:
                return confirm()
            if action_id is not None:
                return {'state': 'pending'}
            if check:
                return {'state': 'retry'}
            params = dict(account_id=account, kind=kind, browse_id=browse,
                          title=detail.get('name') or detail.get('title') or browse)
            action_id = self.actions.record_action(SAVE_CATALOG_ENTITY, json.dumps(params), '{}', 'pending', '{}', self.now())
            try:
                if kind == 'artist':
                    client.subscribe_artist(target)
                else:
                    client.rate_playlist(target, 'LIKE')
            except Exception as exc:
                if getattr(exc, 'outcome', None) == 'not-dispatched':
                    self.actions.update_action(action_id, 'failed', self.now())
                    return {'state': 'retry'}
                self.logger.warning('Catalog library save awaits confirmation: %s %s', kind, browse, exc_info=True)
            # Neither an empty acknowledgement nor a lost response proves a save.
            try:
                saved, _, detail = self._observe(client, kind, browse)
                if saved:
                    return confirm()
            except Exception:
                self.logger.debug('Catalog save readback unavailable', exc_info=True)
            return {'state': 'pending'}
        finally:
            self._writes.release()
