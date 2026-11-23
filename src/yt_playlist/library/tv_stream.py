"""Checkpointed, bounded TV continuations through the generated-playlist writer."""
from hashlib import sha224

from yt_playlist.library.tv_launch import PendingTV, rollover
from yt_playlist.library.playlist_snapshot import validate_playlist
from yt_playlist.library.playlist_writes import account_additions, add_items, PlaylistAddUncertain
from yt_playlist.library.playlist_policy import tv_continuation
from yt_playlist.rec import tv


def refill(store, client, account_id, playlist, recipe, token, request_id, items, now, *,
           destination=None, policy='upload'):
    """Never replay an uncertain append, mutate an adopted/saved source, or grow beyond 60 videos."""
    receipt_key = f'{playlist.id}:{token}:{request_id}'
    if receipt := store.tv.receipt(receipt_key):
        if receipt.get('pending'):
            # Older requests did not save the provider playlist ID.
            raise PendingTV()
        return receipt['playlist_id'], receipt['stream'], receipt['items']
    rollover_id = 'refill:' + sha224(receipt_key.encode()).hexdigest()
    if store.tv.launch(rollover_id):
        return rollover(store, client, account_id, rollover_id, items, now)
    result = _refill(store, client, account_id, playlist, recipe, token, request_id, items, now, rollover_id,
                     destination=destination, policy=policy)
    pid, stream, confirmed = result
    if pid == playlist.id:
        store.tv.complete_receipt(receipt_key, {'playlist_id': pid, 'stream': stream, 'items': confirmed}, now)
        tv.record_draw(store, confirmed)
    return result


def _refill(store, client, account_id, playlist, recipe, token, request_id, items, now, rollover_id, *,
            destination=None, policy='upload'):
    live = (token and token == recipe.get('tv_stream')
            and store.playlists.is_quarantined_playlist(playlist.playlist_scope_id))
    if live:
        previous = recipe.get('tv_refill') or {}
        if previous.get('request_id') == request_id and previous.get('state') == 'complete':
            return playlist.id, token, previous['items']
        if previous.get('state') == 'pending':
            return _confirm(store, client, playlist, recipe, previous, now)
    if not items:
        raise PendingTV()
    if destination is None:
        destination = [(t['identity_key'], t['video_id']) for t in
                       store.charts.playlist_tracks_detail(playlist.id)] if live else []
    old, additions = tv_continuation(items, destination if live else [], recipe=recipe if live else {}, policy=policy)
    if not live or len(old) + len(additions) > tv.MAX_POOL:
        # Opening a saved video playlist never grants permission to alter it.
        # A long stream rolls to another bounded Generated playlist as needed.
        return rollover(store, client, account_id, rollover_id, items, now)
    before = [t['video_id'] for t in store.charts.playlist_tracks_detail(playlist.id)]
    remote = validate_playlist(client.get_playlist(playlist.provider_playlist_id, limit=None))['tracks']
    if [t['videoId'] for t in remote] != before:
        raise ValueError('This TV playlist changed elsewhere. Reset TV to continue without changing that playlist.')
    checkpoint = {'request_id': request_id, 'state': 'pending', 'items': items,
                  'additions': additions, 'before': before, 'at': now}
    recipe = {**recipe, 'videos': old, 'tv_refill': checkpoint}
    store.playlist_recipes.set_recipe(playlist.playlist_scope_id, recipe, now)
    if additions:
        # The same addition/reconciliation boundary as ordinary generated mixes.
        outcome = account_additions(add_items, client, playlist.provider_playlist_id,
                                    [t['video_id'] for t in additions], prior_tracks=remote)
        try:
            outcome.finish()
        except PlaylistAddUncertain:
            raise PendingTV() from None
    return _confirm(store, client, playlist, recipe, checkpoint, now)


def _confirm(store, client, playlist, recipe, checkpoint, now):
    try:
        remote = validate_playlist(client.get_playlist(playlist.provider_playlist_id, limit=None))['tracks']
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
    store.playlist_recipes.set_recipe(playlist.playlist_scope_id, recipe, now)
    return playlist.id, recipe['tv_stream'], checkpoint['items']
