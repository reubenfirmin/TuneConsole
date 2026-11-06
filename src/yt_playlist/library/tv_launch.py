"""Once-only initial TV creation and read-only recovery of its known destination."""
import logging
import uuid

from yt_playlist.core.accounts import client_for
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.library.action_claim import claim_action
from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.library.tv_stream import PendingTV
from yt_playlist.rec import measurement, tv

log = logging.getLogger(__name__)


def _finish(store, state, items, now):
    remote = state['provider_playlist_id']
    playlist = store.playlists.get_playlist_for_account(state['account_id'], remote)
    if playlist is None:
        store.playlists.mark_generated_playlist(remote)
    pid = store.playlists.publish_confirmed_tracks(playlist.id if playlist else None,
        state['title'], items, now, destination=(state['account_id'], remote))
    playlist = store.playlists.get_playlist(pid)
    store.playlist_recipes.set_recipe(playlist.playlist_scope_id, {
        'lane': 'tv', 'media': 'video', 'max_minutes': state['max_minutes'],
        'videos': items, 'tv_stream': state['stream']}, now)
    if state['state'] != 'complete':
        with atomic_write(store):
            tv.record_draw(store, items)
            measurement.record_action(store, 'save', now, playlist_id=pid,
                                      event_id=f'generate:{remote}', generated=True)
            store.rec_proposals.save_tv_launch(state['request_id'], complete=True)
    return pid, state['stream']


def _recover(store, clients, state, now):
    # A deleted/disconnected owner must never fall back to the current main account.
    if state['account_id'] is None:
        raise PendingTV()
    account_id, client = client_for(store, clients, account_id=state['account_id'])
    remote = state['provider_playlist_id']
    if not remote:
        raise PendingTV()
    playlist = store.playlists.get_playlist_for_account(account_id, remote)
    if state['state'] == 'complete' and playlist is not None:
        return playlist.id, state['stream']
    actual = validate_playlist(client.get_playlist(remote, limit=None))['tracks']
    absent = set(state['outcome']['rejected'] + state['outcome']['unattempted'])
    desired = [item for item in state['items'] if item['video_id'] not in absent]
    if not desired or [t['videoId'] for t in actual] != [t['video_id'] for t in desired]:
        raise PendingTV()
    # A complete current read proves delivery; never add, create or remove here.
    return _finish(store, state, desired, now)


def start(store, clients, request_id, items, now):
    # The OS claim also covers a late original request in another app process.
    # Its durable SQL intent survives losing that process and its response.
    with claim_action(store, 'tv-launch:' + request_id, operation='TV launch'):
        if state := store.rec_proposals.tv_launch(request_id):
            return _recover(store, clients, state, now)
        account_id, client = client_for(store, clients)
        maximum, stream = tv.max_minutes(store), uuid.uuid4().hex
        if not store.rec_proposals.begin_tv_launch(request_id, account_id, stream, maximum, items, now):
            return _recover(store, clients, store.rec_proposals.tv_launch(request_id), now)
        recipe = {'lane': 'tv', 'media': 'video', 'max_minutes': maximum, 'videos': items, 'tv_stream': stream}
        try:
            result = create_generated_playlist(store, 'TV', items, client, now, account_id, recipe=recipe,
                max_duration_s=float('inf') if maximum is None else maximum * 60,
                on_created=lambda remote, title: store.rec_proposals.save_tv_launch(
                    request_id, destination=(remote, title)))
        except PlaylistAddUncertain as error:
            store.rec_proposals.save_tv_launch(request_id, outcome={
                'confirmed': error.confirmed, 'rejected': error.skipped,
                'uncertain': error.uncertain, 'unattempted': error.unattempted})
            raise PendingTV() from error
        except Exception:
            # Even a lost create response leaves an admitted unresolved intent.
            log.warning('Initial TV creation not confirmed: %s', request_id, exc_info=True)
            raise PendingTV() from None
        confirmed = {item['video_id'] for item in result['tracks']}
        store.rec_proposals.save_tv_launch(request_id, outcome={
            'confirmed': confirmed, 'rejected': [t['video_id'] for t in items if t['video_id'] not in confirmed]})
        return _finish(store, store.rec_proposals.tv_launch(request_id), result['tracks'], now)
