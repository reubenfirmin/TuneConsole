"""Admit and project playback observations without transport or provider access."""
from copy import deepcopy
from dataclasses import dataclass
import json
import math

from yt_playlist.core.player_frames import PLAYBACK_KINDS, CURATION_KINDS, _curation_fields, _PAYLOAD_EXTRAS, _RATE_STATUS, _BODY_CAP
from yt_playlist.util.curation import confirmed_rate
from yt_playlist.util.matching import identity_key


def player_snapshot(msg, previous=None):
    """Project current sensor presence without borrowing a stale queue item."""
    snapshot = {"title": msg.get("title"), "artist": msg.get("artist"),
                "thumbnail": msg.get("thumbnail"), "likeStatus": msg.get("likeStatus"),
                "video_id": msg.get("videoId"), "paused": bool(msg.get("paused")),
                "metadata_verified": msg.get("metadataVerified")
                if type(msg.get("metadataVerified")) is bool else None}
    for field, key in (("muted", "muted"), ("tabMuted", "tab_muted")):
        if field in msg:
            snapshot[key] = bool(msg[field])
    position = msg.get('position')
    snapshot['position'] = position if (type(position) in (int, float) and math.isfinite(position)
                                        and position >= 0) else None
    # Older sensors report a buffered endpoint. Full duration belongs to the
    # exact upload's catalog metadata, resolved when serving player status.
    snapshot['duration'] = None
    for field, key in (('brandId', 'brand_id'), ('brandKnown', 'brand_known'),
                       ('playlist', 'playlist_ytm_id'), ('albumBrowseId', 'album_browse_id')):
        if field in msg:
            snapshot[key] = (msg.get(field) or '') if field in ('playlist', 'albumBrowseId') else msg[field]
        elif previous and previous.get('video_id') == snapshot['video_id'] and key in previous:
            snapshot[key] = previous[key]
    item = msg.get('queueItem')
    if (snapshot['video_id'] and isinstance(item, dict) and isinstance(item.get('videoId'), str)
            and 0 < len(item['videoId']) <= 128 and item.get('playlist')
            and item['playlist'] == snapshot.get('playlist_ytm_id')):
        snapshot['queue_item'] = {'video_id': item['videoId'], 'playlist': item['playlist']}
        if type(item.get('index')) is int and 0 <= item['index'] < 100000:
            snapshot['queue_item']['index'] = item['index']
    return snapshot


def account_for_brand(accounts, brand):
    matches = [a.id for a in accounts if (a.brand_account_id or '') == (brand or '')]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class Observation:
    kind: str
    account_id: int
    body: dict
    at: float


def admit(kind, accounts, body, now):
    if kind == 'live':
        if (body.get('brandKnown') is False or body.get('paused') or body.get('muted')
                or body.get('tabMuted') or body.get('volume') == 0):
            return None
    else:
        event = (body.get('kind') or '').strip()
        if event not in PLAYBACK_KINDS | CURATION_KINDS:
            return None
        if (event in CURATION_KINDS or event == 'next') and body.get('brandKnown') is not True:
            return None
    account = account_for_brand(accounts, (body.get('brandId') or '').strip())
    return Observation(kind, account, deepcopy(body), now) if account is not None else None


def player_projection(body):
    """Interpret an admitted frame without repositories or worker callbacks."""
    kind = (body.get('kind') or '').strip()
    if kind in PLAYBACK_KINDS:
        video, playlist, action = body.get('videoId') or None, body.get('playlist') or None, None
        position, duration = body.get('position'), body.get('duration')
        payload = {key: body[key] for key in _PAYLOAD_EXTRAS if body.get(key) not in (None, '')}
    else:
        video, playlist, action = _curation_fields(kind, body)
        position = duration = None
        payload = {'url': body.get('url'), 'body': (body.get('body') or '')[:_BODY_CAP], 'href': body.get('href')}
        if action:
            payload['action'] = action
        payload.update({key: body[key] for key in ('phase', 'success', 'httpStatus', 'error') if key in body})
    return dict(kind=kind, video=video, playlist=playlist, position=position, duration=duration,
        payload=payload, exit=kind in {'track_exit', 'ended', 'bye'},
        rating=_RATE_STATUS[action] if kind == 'rate' and confirmed_rate(body) and video and action in _RATE_STATUS else None)


class PlaybackObservations:
    def __init__(self, accounts, tracks, history, settings, events, *, deferred, capture,
                 album_context, record_playback, capture_start, observe_exit,
                 rating_observations, confirmed_rating, reconcile_ratings, refresh_ratings):
        self.accounts, self.tracks, self.history, self.settings, self.events = accounts, tracks, history, settings, events
        self.deferred, self.capture = deferred, capture
        self.album_context, self.record_playback, self.capture_start = album_context, record_playback, capture_start
        self.observe_exit, self.rating_observations = observe_exit, rating_observations
        self.confirmed_rating, self.reconcile_ratings, self.refresh_ratings = confirmed_rating, reconcile_ratings, refresh_ratings

    def observe(self, kind, body, now):
        observation = admit(kind, self.accounts.get_accounts(), body, now)
        if observation is None:
            return False
        if self.deferred():
            self.capture(kind, observation.account_id, observation.body, now)
            return True
        return self.project(observation)

    def project(self, observation, *, inline_analytics=False, capture_id=None):
        if observation.kind == 'live':
            return self._live(observation, inline_analytics)
        return self._player(observation, capture_id)

    def _live(self, observation, inline):
        body, account, now = observation.body, observation.account_id, observation.at
        title, artist = (body.get('title') or '').strip(), (body.get('artist') or '').strip()
        verified = body.get('metadataVerified') is not False
        video = body.get('videoId')
        if not verified:
            key = self.tracks.identity_key_for_video(video) if body.get('videoVerified') is True and video else None
            if not key:
                return False
        else:
            if not title:
                return False
            key = self.tracks.observe_track(video, title, artist, now=now) if video else identity_key(title, artist)
        status = (body.get('likeStatus') or '').strip() if verified else ''
        rating = self.tracks.item_rating(account, video)
        confirmed = rating is not None and rating['source'] == 'api'
        if confirmed:
            status = rating['status']
        context = self.album_context(body, now, account_id=account)
        new = self.history.record_play_event(account, key, video or None, now,
            playlist_ytm_id=body.get('playlist') or None, like_status=status or None,
            source_timezone=body.get('timeZone'), context_choice=bool(context))
        if new:
            self.settings.set_setting('last_plays_sync_at', str(now))
            self.record_playback(body, now, account_id=account)
            initial = rating['status'] if rating else status if status in {'LIKE', 'DISLIKE'} else None
            self.capture_start(account, video, body.get('playlist') or None, initial, now,
                               album=bool(context), inline=inline)
        if status in {'LIKE', 'DISLIKE'} and video:
            if confirmed:
                self.reconcile_ratings({key})
            else:
                self.rating_observations(account, {video: status}, source='action', now=now)
        return new

    def _player(self, observation, capture_id):
        body, account, now = observation.body, observation.account_id, observation.at
        projected = player_projection(body)
        kind, video, playlist = projected['kind'], projected['video'], projected['playlist']
        position, duration, payload = projected['position'], projected['duration'], projected['payload']
        event_id = self.events.record_player_event(account, kind, video, position, duration, playlist,
            json.dumps(payload) if payload else None, now, capture_id=capture_id)
        if projected['exit']:
            self.observe_exit(account, playlist, video, kind, position, duration, event_id, now)
        if projected['rating'] is not None:
            self.confirmed_rating(account, video, projected['rating'], now)
            self.refresh_ratings()
        return True
