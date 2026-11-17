"""TV application workflows. Inspection only acquires preview frames; it never plays."""
import json
import math
import re
import uuid
from threading import Lock

from yt_playlist.library.tv_stream import PendingTV


def playback_message(playlist, items, accounts, form):
    kind = form.get('kind')
    item = next((t for t in items if t['video_id'] == form.get('video_id')), None)
    if kind not in {'play', 'state', 'tick', 'track_exit', 'ended', 'bye', 'player_error'} or item is None:
        raise ValueError('Invalid video playback event.')
    identity = next((a for a in accounts if a.id == playlist.account_id), None)
    if identity is None:
        raise LookupError('Playlist account not found.')
    try:
        position = float(form.get('position') or 0)
        if not math.isfinite(position) or position < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('Invalid playback position.') from None
    return {'kind': kind, 'videoId': item['video_id'], 'title': item['title'],
        'artist': item['artist'], 'playlist': playlist.provider_playlist_id,
        'brandId': identity.brand_account_id or '', 'brandKnown': True,
        'position': position, 'duration': item['duration'], 'state': form.get('state'),
        'timeZone': form.get('time_zone')}


class TVCommands:
    def __init__(self, playlists, recipes, accounts, settings, sessions, proposals, *,
                 contents, status, queue, preparation_message, cached, artwork, request_artwork,
                 inspect, access, start, refill, refresh, observe, player, paused, now, logger):
        self.playlists, self.recipes, self.accounts = playlists, recipes, accounts
        self.settings, self.sessions, self.proposals = settings, sessions, proposals
        self.contents, self.query_status, self.queue = contents, status, queue
        self.preparation_message, self.cached = preparation_message, cached
        self.artwork, self.request_artwork, self.inspect = artwork, request_artwork, inspect
        self.access, self.start_session, self.refill_session, self.refresh = access, start, refill, refresh
        self.observe, self.player, self.paused, self.now, self.logger = observe, player, paused, now, logger
        self.refill_lock = Lock()

    def status(self):
        return self.query_status()

    def parameters(self, value):
        try:
            maximum = None if value == 'unlimited' else int(value)
        except (ValueError, TypeError):
            raise ValueError('Choose a video length or Unlimited.') from None
        if maximum is not None and maximum < 1:
            raise ValueError('Choose at least one minute, or Unlimited.')
        self.settings.set_setting('tv_max_minutes', 'unlimited' if maximum is None else str(maximum))
        return self.status()

    def artist_artwork(self, artist):
        if not artist or len(artist) > 200:
            return {'images': [], 'pending': False}
        images, prepared = self.artwork(artist)
        pending = not prepared and self.request_artwork(artist)
        if pending:
            self.refresh()
        return {'images': images, 'pending': pending}

    def visual_check(self, pid, video_id):
        contents = self.contents(pid, artwork=False)
        if contents is None:
            raise LookupError('Video playlist not found.')
        _, items = contents
        item = next((item for item in items if item['video_id'] == video_id), None)
        if item is None:
            raise ValueError('Video not in this playlist.')
        if self.paused():
            return {'preparing': True}
        _, client = self.access.resolve(playlist_id=pid)
        result = self.inspect(client, item, self.now())
        return {'preparing': True} if result is None else {'video_id': video_id, **result}

    def play(self, request_id):
        if request_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            raise ValueError('Invalid TV request.')
        if not request_id:
            account = next((a for a in self.accounts.get_accounts() if a.is_master), None)
            request_id = (self.sessions.pending_launch(account.id) if account else None) or uuid.uuid4().hex
        previous = self.sessions.launch(request_id)
        items = [] if previous else self.queue()
        if not previous and not items:
            self.refresh()
            message = self.preparation_message()
            return {'preparing': True, **({'message': message} if message else {})}
        try:
            pid, stream = self.start_session(request_id, items, self.now())
            playlist, videos = self.contents(pid)
            state = self.sessions.launch(request_id)
            wanted = set(state['outcome']['confirmed'])
            return {'playlist_id': playlist.id, 'title': playlist.title,
                'items': [t for t in videos if t['video_id'] in wanted], 'stream': stream}
        except PendingTV:
            return {'pending': True, 'request_id': request_id}
        except Exception:
            self.logger.exception('TV playlist creation failed')
            return {'pending': True, 'request_id': request_id}

    def reset(self):
        if pool := self.cached():
            self.proposals.put_proposals('tv_pool', {**pool, 'retry_at': 0, 'state': 'searching'}, self.now())
        self.refresh()
        return {'ok': True}

    def refill(self, pid, token, request_id, exclude, recent, current):
        playlist = self.playlists.get_playlist(pid)
        recipe = self.recipes.get_recipe(playlist.playlist_scope_id) if playlist else None
        if not isinstance(recipe, dict) or recipe.get('media') != 'video':
            raise LookupError('This TV playlist is no longer available.')
        if not self.refill_lock.acquire(blocking=False):
            return {'preparing': True}
        try:
            account, client = self.access.resolve(playlist_id=pid)
            items = self.queue(exclude=exclude, recent=recent, current=current, limit=4)
            next_pid, stream, confirmed = self.refill_session(client, account, playlist, recipe,
                token, request_id, items, self.now())
            next_playlist, videos = self.contents(next_pid)
            metadata = {t['video_id']: t for t in videos}
            self.refresh()
            return {'playlist_id': next_pid, 'title': next_playlist.title,
                'items': [metadata[t['video_id']] for t in confirmed if t['video_id'] in metadata], 'stream': stream}
        except PendingTV:
            self.refresh()
            return {'preparing': True}
        finally:
            self.refill_lock.release()

    def saved(self, pid):
        contents = self.contents(pid)
        if contents is None:
            raise LookupError('This video playlist is no longer available.')
        playlist, items = contents
        return {'playlist_id': playlist.id, 'title': playlist.title, 'items': items}

    def event(self, pid, form):
        if self.paused():
            return {'ok': True}
        contents = self.contents(pid, artwork=False)
        if contents is None:
            raise LookupError('Video playlist not found.')
        playlist, items = contents
        msg = playback_message(playlist, items, self.accounts.get_accounts(), form)
        if msg['kind'] == 'player_error':
            try:
                code = int(msg['state'])
                if not 0 <= code <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError('Invalid player error code.') from None
            self.logger.warning('TV player error: playlist=%s video=%s code=%s position=%.3f',
                pid, msg['videoId'], code, msg['position'])
        else:
            self.observe('live' if msg['kind'] == 'play' else 'player', msg, self.now())
        return {'ok': True}

    async def takeover(self):
        try:
            status, body = await self.player.request_control({'type': 'pause-for-tv'}, timeout=8)
            result = json.loads(body)
            if status == 200 and result.get('ok') and result.get('paused') is True:
                return {'ok': True}
        except Exception:
            self.logger.warning('TV playback handoff was not confirmed', exc_info=True)
        return {'error': 'Audio playback could not be paused. Try TV again.'}
