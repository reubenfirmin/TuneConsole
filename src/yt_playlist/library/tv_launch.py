"""Recoverable playlist creation for TV launches and continuations."""
import logging
import uuid

from yt_playlist.core.accounts import client_for
from yt_playlist.library.action_claim import claim_action
from yt_playlist.library.generated_composition import commands as generated_commands
from yt_playlist.library.playlist_policy import generated_memberships
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.rec import measurement, tv

log = logging.getLogger(__name__)


class PendingTV(Exception):
    """A provider read has not confirmed the previous write yet."""


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
        tv.record_draw(store, items)
        measurement.record_action(store, 'save', now, playlist_id=pid,
                                  event_id=f'generate:{remote}', generated=True)
        store.tv.complete_launch(state['request_id'], [item['video_id'] for item in items])
    return pid, state['stream'], items


def _recover(store, client, account_id, state, now):
    if state['account_id'] != account_id:
        raise PendingTV()
    remote = state['provider_playlist_id']
    if not remote:
        raise PendingTV()
    playlist = store.playlists.get_playlist_for_account(account_id, remote)
    if state['state'] == 'complete' and playlist is not None:
        confirmed = set(state['outcome']['confirmed'])
        return playlist.id, state['stream'], [item for item in state['items'] if item['video_id'] in confirmed]
    try:
        actual = validate_playlist(client.get_playlist(remote, limit=None))['tracks']
    except Exception:
        log.debug('TV playlist readback unavailable: %s', remote, exc_info=True)
        raise PendingTV() from None
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
        if state := store.tv.launch(request_id):
            # A disconnected owner cannot fall back to the current main account.
            if state['account_id'] is None:
                raise PendingTV()
            account_id, client = client_for(store, clients, account_id=state['account_id'])
            pid, stream, _items = _recover(store, client, account_id, state, now)
        else:
            account_id, client = client_for(store, clients)
            pid, stream, _items = _create(store, client, account_id, request_id, items, now, current=True)
        return pid, stream


def rollover(store, client, account_id, request_id, items, now):
    with claim_action(store, 'tv-launch:' + request_id, operation='TV continuation'):
        if state := store.tv.launch(request_id):
            return _recover(store, client, account_id, state, now)
        return _create(store, client, account_id, request_id, items, now, current=False)


def _create(store, client, account_id, request_id, items, now, *, current):
    maximum, stream = tv.max_minutes(store), uuid.uuid4().hex
    limit = float('inf') if maximum is None else maximum * 60
    items = generated_memberships(items, maximum=limit)
    if not items:
        raise PendingTV()
    if not store.tv.begin_launch(request_id, account_id, stream, maximum, items, now, current=current):
        return _recover(store, client, account_id, store.tv.launch(request_id), now)
    recipe = {'lane': 'tv', 'media': 'video', 'max_minutes': maximum, 'videos': items, 'tv_stream': stream}
    try:
        result = generated_commands(store, now).create('TV', items, client=client, account_id=account_id, recipe=recipe,
            max_duration_s=limit, on_created=lambda remote, title: store.tv.save_launch(
                request_id, destination=(remote, title)))
    except PlaylistAddUncertain as error:
        store.tv.save_launch(request_id, outcome={
            'confirmed': error.confirmed, 'rejected': error.skipped,
            'uncertain': error.uncertain, 'unattempted': error.unattempted})
        raise PendingTV() from error
    except Exception:
        log.warning('TV playlist creation not confirmed: %s', request_id, exc_info=True)
        raise PendingTV() from None
    confirmed = {item['video_id'] for item in result['tracks']}
    store.tv.save_launch(request_id, outcome={
        'confirmed': confirmed, 'rejected': [t['video_id'] for t in items if t['video_id'] not in confirmed]})
    return _finish(store, store.tv.launch(request_id), result['tracks'], now)
