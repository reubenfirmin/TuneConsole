"""Complete playback actions over player, library and retained-feedback capabilities."""
import asyncio
import json
import math
import time
from urllib.parse import parse_qs, urlparse
from yt_playlist.util.navigation import album_url, playlist_url


class PlaybackCommands:
    def __init__(self, player, accounts, tracks, playlists, recipes, collection, *,
                 radio, work, now, ops, rating, identity, duration, record_next,
                 paused, measure, logger, monotonic=time.monotonic):
        self.player, self.accounts, self.tracks = player, accounts, tracks
        self.playlists, self.recipes, self.collection = playlists, recipes, collection
        self.radio, self.work, self.now, self.ops = radio, work, now, ops
        self.rating, self.identity, self.duration, self.record_next = rating, identity, duration, record_next
        self.paused, self.measure, self.logger = paused, measure, logger
        self.monotonic = monotonic

    def admit(self):
        return self.player.begin_playback_intent()

    def status(self):
        bridge = self.player
        session = self.radio.state() if self.radio is not None else {}
        playing, seen = bridge.now_playing, getattr(bridge, 'now_playing_seen_at', None)
        if playing is not None and seen is not None and self.monotonic() - seen > 8:
            bridge.now_playing = playing = None
            bridge.now_playing_seen_at = None
        if playing is not None:
            playing = {**playing, 'duration': None}
            if self.tracks is not None:
                duration = self.tracks.duration_for_video(playing.get('video_id'))
                if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                    playing['duration'] = duration
                rating = self.rating(playing.get('video_id'))
                if rating:
                    playing['likeStatus'] = rating['status']
                if not playing.get('thumbnail') and playing.get('video_id'):
                    thumbnail = self.tracks.thumbnail_for_video(playing['video_id'])
                    if thumbnail:
                        playing['thumbnail'] = thumbnail
            source = self.collection.playback_source(playing.get('playlist_ytm_id'),
                playing.get('video_id'), playing.get('album_browse_id')) if self.collection else None
            if source:
                url = playlist_url(source['id']) if source['kind'] == 'playlist' else album_url(source['browse'])
                source = dict(kind=source['kind'], title=source['title'], url=url, external=False)
            if session.get('active') and session.get('playlist_id') == playing.get('playlist_ytm_id'):
                source = dict(kind='radio', title=session['title'], url='/#radio', external=False)
            playing = {**playing, 'source': source, 'snapshot_age': max(0, self.monotonic() - seen) if seen else 0}
        return dict(connected=bridge.connected, now_playing=playing, radio_resume=session.get('resume_player'),
            resumable_player=bridge.resumable_player if bridge.connected and bridge.resumable_player
                and bridge.resumable_player.get('paused') else None,
            sensor_health=getattr(bridge, 'sensor_health', None), radio=bool(session.get('active')),
            radio_ready=bool(session.get('active') and not session.get('starting')))

    async def rate(self, action, video_id, like_status=None):
        if like_status is None:
            rating = await asyncio.to_thread(self.rating, video_id)
            playing = self.player.now_playing or {}
            current = rating['status'] if rating else playing.get('likeStatus') if playing.get('video_id') == video_id else None
            want = 'LIKE' if action == 'like' else 'DISLIKE'
            like_status = 'INDIFFERENT' if current == want else want
        loop = asyncio.get_running_loop()
        def mirror(account_id):
            return asyncio.run_coroutine_threadsafe(self.mirror_rating(action, video_id, like_status, account_id), loop).result()
        return await self.work.to_thread(self.ops().set_rating, video_id, like_status, after_confirmed=mirror)

    def play(self, url, intent):
        try:
            query = parse_qs(urlparse(url).query)
            playlist = (query.get('list') or [''])[0].removeprefix('RDAMPL')
            if playlist:
                for pl in self.playlists.get_playlists():
                    if pl.provider_playlist_id == playlist and (self.recipes.get_recipe(pl.playlist_scope_id) or {}).get('media') == 'video':
                        return {'ok': True, 'tv_url': f'/?tv_playlist={pl.id}#tv'}
            self.player.send_control({"type": "navigate", "url": url}, intent=intent)
            video_id = (query.get("v") or [None])[0]
            if video_id:
                self.measure( "start", self.now(), video_id=video_id)
            elif query.get("list"):
                playlist = query["list"][0].removeprefix("RDAMPL")
                for pl in self.playlists.get_playlists():
                    if pl.provider_playlist_id == playlist:
                        self.measure( "start", self.now(), playlist_id=pl.id)
                        break
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def mirror_rating(self, action, video_id, like_status, account_id):
        # The API confirms the exact upload/account even if playback has moved on. When it
        # is still playing, also drive Music's native control so its selected thumb follows
        # the saved rating. Radio repairs its saved future before advancing.
        playing = self.player.now_playing or {}
        if self.player.connected and playing.get("video_id") == video_id:
            player_failed = False
            native_diagnostics = None
            identity = None
            try:
                identities = await asyncio.to_thread(self.accounts.get_accounts)
                identity = next(i for i in identities if i.id == account_id)
                status, body = await self.player.request_control({
                    "type": "rate", "action": action, "videoId": video_id,
                    "likeStatus": like_status, "brandId": identity.brand_account_id or "",
                    "skip": False}, timeout=10)
                result = json.loads(body)
                native_diagnostics = result.get("diagnostics")
                if status != 200 or not result.get("ok"):
                    raise ValueError(result.get("error") or "Player command failed")
            except Exception:
                self.logger.warning("rating saved but native thumb update failed: video=%s diagnostics=%s",
                               video_id, json.dumps(native_diagnostics, ensure_ascii=True)[:4000], exc_info=True)
                player_failed = True
            # A missing native rating control must not prevent the requested
            # skip after the exact song's dislike has already been saved.
            if like_status == "DISLIKE" and identity is not None:
                try:
                    skipped = await self.skip(video_id, brand_id=identity.brand_account_id or "")
                    if not skipped.get("ok"):
                        raise ValueError(skipped.get("error") or "The player did not advance")
                except Exception:
                    self.logger.warning("rating saved but skip failed: video=%s", video_id, exc_info=True)
                    player_failed = True
            if player_failed:
                return {"ok": True, "error": "", "player_error": "Player update not confirmed"}
        return {"ok": True, "error": ""}

    async def skip(self, video_id, *, brand_id=None):
        playing, now = dict(self.player.now_playing or {}), self.now()
        if (brand_id is not None and playing.get('brand_known') is True
                and (playing.get('brand_id') or '') != brand_id):
            return {'ok': False, 'error': 'Player account is unavailable or different'}
        if brand_id is None and playing.get('brand_known') is True:
            brand_id = playing.get('brand_id') or ''
        if brand_id is not None and getattr(self.player, 'account_bound_next', None) is not True:
            return {'ok': False, 'error': 'Update the TuneConsole extension before skipping for this account'}
        radio = self.radio
        if radio is not None and not self.paused():
            options = {'brand_id': brand_id} if brand_id is not None else {}
            result = await self.work.to_thread(radio.skip, video_id, **options)
            if result is not None:
                return result
        # Gather optional catalog context before sending the command. A
        # confirmed Next must reach its durable inbox even if replacement
        # closes every main handle while the extension is responding.
        identity, duration = None, None
        if playing.get('video_id') == video_id and playing.get('brand_known') is True:
            identity = self.identity( playing.get('brand_id') or '')
            if identity is not None:
                from yt_playlist.core.db_space import StorageCompacting
                try:
                    duration = self.duration(video_id)
                except StorageCompacting:
                    pass  # Unknown duration remains explicit in the retained observation.
        frame = {"type": "next", "videoId": video_id}
        if brand_id is not None:
            frame['brandId'] = brand_id
        status, body = await self.player.request_control(frame, timeout=10)
        result = json.loads(body)
        if status != 200 or not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "The player did not advance"}
        if identity is not None:
            def retain():
                self.record_next( identity, video_id, playing.get('position'),
                    duration, playing.get('playlist_ytm_id'), now)
            try:
                await asyncio.to_thread(retain)
            except Exception:
                self.logger.warning('Player advanced but local Next capture failed: video=%s', video_id, exc_info=True)
                return {'ok': True, 'player_error': 'Playback advanced, but this skip could not be saved to local history.'}
        return {"ok": True}

    async def toggle(self, action: str = "toggle", resume: bool = False, videoId: str = "",
                                 confirm: bool = False):
        if action not in ("toggle", "play", "pause"):
            return {"ok": False, "error": "invalid playback action"}
        frame = {"type": "playpause"}
        if action != "toggle":
            frame["action"] = action
        if resume:
            player = self.player.resumable_player
            if not self.player.connected or not player or not player.get("paused") or not videoId or \
                    player.get("video_id") != videoId:
                return {"ok": False, "error": "Paused player is no longer available"}
            frame.update(resumeOnly=True, videoId=videoId)
        if confirm:
            if action not in ("play", "pause") or not videoId:
                return {"ok": False, "error": "A displayed song and playback action are required"}
            player = self.player.resumable_player if resume else self.player.now_playing
            if not player or player.get("video_id") != videoId:
                return {"ok": False, "error": "The playing song changed. Try again."}
            frame["videoId"] = videoId
            paused = action == "pause"
            started = self.monotonic()
            connection_id = self.player.connection_id
            command = asyncio.create_task(self.player.request_control(frame, timeout=5))
            try:
                while True:
                    done, _ = await asyncio.wait({command}, timeout=.05)
                    if done:
                        status, body = command.result()
                        result = json.loads(body)
                        if (status == 200 and result.get("ok") and result.get("paused") is paused
                                and result.get("videoId") == videoId):
                            return {"ok": True, "paused": paused}
                        return {"ok": False, "error": result.get("error") or "Playback was not confirmed"}
                    # Identified store builds without control acknowledgements report actual media
                    # state. Accept a fresh matching observation, never just a queued send, and
                    # never resend a toggle whose delivery is uncertain. A new connection's
                    # unreported capability must still wait for its native acknowledgement.
                    observed = self.player.now_playing
                    if (self.player.playback_confirmation is False and self.player.connection_id == connection_id
                            and self.player.connected and observed
                            and observed.get("video_id") == videoId and observed.get("paused") is paused
                            and (self.player.now_playing_seen_at or 0) > started):
                        return {"ok": True, "paused": paused}
            except Exception:
                self.logger.warning("playback not confirmed: action=%s video=%s", action, videoId, exc_info=True)
                return {"ok": False, "error": "Playback was not confirmed. Open YouTube Music and try again."}
            finally:
                command.cancel()
                try:
                    await command
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            self.player.send_control(frame)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
