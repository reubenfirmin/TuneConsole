"""Checkpointed, bounded TV continuations through the generated-playlist writer."""
import uuid

from yt_playlist.library.playlist_generated import create_generated_playlist
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_writes import account_additions, add_items, PlaylistAddUncertain
from yt_playlist.rec import tv


class PendingTV(Exception):
    """A provider read has not confirmed the previous write yet."""


def refill(store, client, identity_id, playlist, recipe, token, request_id, items, now):
    """Never replay an uncertain append, mutate an adopted/saved source, or grow beyond 60 videos."""
    receipt_key = f'{playlist.id}:{token}:{request_id}'
    receipts = store.rec_proposals.get_proposals('tv_continuations') or {}
    if receipt := receipts.get(receipt_key):
        return receipt['playlist_id'], receipt['stream'], receipt['items']
    result = _refill(store, client, identity_id, playlist, recipe, token, request_id, items, now)
    # Retain the response at the original request address as well as the target
    # playlist. Retrying a lost rollover response must not create another list.
    pid, stream, confirmed = result
    receipts[receipt_key] = {'playlist_id': pid, 'stream': stream, 'items': confirmed}
    store.rec_proposals.put_proposals('tv_continuations', dict(list(receipts.items())[-60:]), now)
    tv.record_draw(store, confirmed)
    return result


def _refill(store, client, identity_id, playlist, recipe, token, request_id, items, now):
    live = (token and token == recipe.get('tv_stream')
            and store.playlists.is_quarantined_playlist(playlist.scope_ref))
    if live:
        previous = recipe.get('tv_refill') or {}
        if previous.get('request_id') == request_id and previous.get('state') == 'complete':
            return playlist.id, token, previous['items']
        if previous.get('state') == 'pending':
            return _confirm(store, client, playlist, recipe, previous, now)
    if not items:
        raise PendingTV()
    membership = {t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)} if live else set()
    old = [t for t in recipe.get('videos', []) if t['video_id'] in membership] if live else []
    known = {t['video_id'] for t in old}
    additions = [t for t in items if t['video_id'] not in known]
    if not live or len(old) + len(additions) > tv.MAX_POOL:
        # Opening a saved video playlist never grants permission to alter it.
        # A long stream rolls to another bounded Generated playlist as needed.
        token = uuid.uuid4().hex
        recipe = {'lane': 'tv', 'media': 'video', 'tv_stream': token,
                  'max_minutes': tv.max_minutes(store), 'videos': items,
                  'tv_refill': {'request_id': request_id, 'state': 'complete', 'items': items}}
        limit = tv.max_minutes(store)
        result = create_generated_playlist(store, 'TV', items, client, now, identity_id, recipe=recipe,
                                           max_duration_s=float('inf') if limit is None else limit * 60)
        confirmed = {t['video_id'] for t in store.charts.playlist_tracks_detail(result['pid'])}
        accepted = [t for t in items if t['video_id'] in confirmed]
        store.playlist_recipes.set_recipe(result['new_ytm'], {**recipe, 'videos': accepted,
            'tv_refill': {'request_id': request_id, 'state': 'complete', 'items': accepted}}, now)
        return result['pid'], token, accepted
    before = [t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)]
    remote = validate_playlist(client.get_playlist(playlist.ytm_playlist_id, limit=None))['tracks']
    if [t['videoId'] for t in remote] != before:
        raise ValueError('This TV playlist changed elsewhere. Reset TV to continue without changing that playlist.')
    checkpoint = {'request_id': request_id, 'state': 'pending', 'items': items,
                  'additions': additions, 'before': before, 'at': now}
    recipe = {**recipe, 'videos': old, 'tv_refill': checkpoint}
    store.playlist_recipes.set_recipe(playlist.scope_ref, recipe, now)
    if additions:
        # The same addition/reconciliation boundary as ordinary generated mixes.
        outcome = account_additions(add_items, client, playlist.ytm_playlist_id,
                                    [t['video_id'] for t in additions], prior_tracks=remote)
        try:
            outcome.finish()
        except PlaylistAddUncertain:
            raise PendingTV() from None
    return _confirm(store, client, playlist, recipe, checkpoint, now)


def _confirm(store, client, playlist, recipe, checkpoint, now):
    try:
        remote = validate_playlist(client.get_playlist(playlist.ytm_playlist_id, limit=None))['tracks']
    except ValueError:
        if now - checkpoint['at'] < 30:
            raise PendingTV() from None
        raise ValueError('YouTube has not confirmed the next videos. Reset TV to start a fresh session.') from None
    before = checkpoint['before']
    actual = [t['videoId'] for t in remote]
    desired = before + [t['video_id'] for t in checkpoint['additions']]
    if actual != desired:
        if now - checkpoint['at'] < 30:
            raise PendingTV()
        raise ValueError('YouTube has not confirmed the next videos. Reset TV to start a fresh session.')
    metadata = {t['video_id']: t for t in [*recipe['videos'], *checkpoint['items']]}
    confirmed = [metadata[video] for video in actual]
    store.playlists.publish_confirmed_tracks(playlist.id, playlist.title, confirmed, now, expected_prefix=before)
    recipe = {**recipe, 'videos': confirmed, 'tv_refill': {
        'request_id': checkpoint['request_id'], 'state': 'complete', 'items': checkpoint['items']}}
    store.playlist_recipes.set_recipe(playlist.scope_ref, recipe, now)
    return playlist.id, recipe['tv_stream'], checkpoint['items']
