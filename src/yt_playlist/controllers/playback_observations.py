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
        if kind == 'next' and body.get('ratingAdvance') is True:
            payload['rating_advance'] = True
    else:
        video, playlist, action = _curation_fields(kind, body)
        position = duration = None
        payload = {'url': body.get('url'), 'body': (body.get('body') or '')[:_BODY_CAP], 'href': body.get('href')}
        if action:
            payload['action'] = action
        payload.update({key: body[key] for key in ('phase', 'success', 'httpStatus', 'error') if key in body})
        requested_at = body.get('requestedAt')
        if kind == 'rate' and type(requested_at) is int and 0 < requested_at <= 2**53 - 1:
            payload['requestedAt'] = requested_at
    return dict(kind=kind, video=video, playlist=playlist, position=position, duration=duration,
        payload=payload, exit=kind in {'track_exit', 'ended', 'bye'},
        rating=_RATE_STATUS[action] if kind == 'rate' and confirmed_rate(body) and video and action in _RATE_STATUS else None)


class PlaybackObservations:
    def __init__(self, accounts, tracks, history, settings, events, *, deferred, capture,
                 album_context, record_playback, capture_start, observe_exit,
                 rating_observations, confirmed_rating, observe_like, reconcile_ratings, refresh_ratings,
                 attributed=None):
        self.accounts, self.tracks, self.history, self.settings, self.events = accounts, tracks, history, settings, events
        self.deferred, self.capture = deferred, capture
        self.album_context, self.record_playback, self.capture_start = album_context, record_playback, capture_start
        self.observe_exit, self.rating_observations = observe_exit, rating_observations
        self.confirmed_rating, self.reconcile_ratings, self.refresh_ratings = confirmed_rating, reconcile_ratings, refresh_ratings
        self.observe_like = observe_like
        self.attributed = attributed or (lambda body, now, account, album: bool(album))

    def observe(self, kind, body, now):
        observation = admit(kind, self.accounts.get_accounts(), body, now)
        if observation is None:
            return False
        if self.deferred():
            self.capture(kind, observation.account_id, observation.body, now)
            return True
        return self.project(observation)

    def project(self, observation, *, inline_analytics=False, capture_id=None, attribute=True):
        if observation.kind == 'live':
            key = self.live_subject(observation.body, observation.at)
            return self.project_live(observation, key, inline_analytics=inline_analytics, attribute=attribute)
        return self._player(observation, capture_id, attribute=attribute)

    def live_subject(self, body, now):
        """Resolve catalog metadata before a replay opens its projection transaction."""
        title, artist = (body.get('title') or '').strip(), (body.get('artist') or '').strip()
        video = body.get('videoId')
        if body.get('metadataVerified') is False:
            return self.tracks.identity_key_for_video(video) if body.get('videoVerified') is True and video else None
        if title:
            return self.tracks.observe_track(video, title, artist, now=now) if video else identity_key(title, artist)

    def project_live(self, observation, key, *, inline_analytics=False, attribute=True):
        if not key:
            return False
        body, account, now = observation.body, observation.account_id, observation.at
        verified, video = body.get('metadataVerified') is not False, body.get('videoId')
        status = (body.get('likeStatus') or '').strip() if verified else ''
        rating = self.tracks.item_rating(account, video)
        confirmed = rating is not None and rating['source'] == 'api'
        if confirmed:
            status = rating['status']
        context = self.album_context(body, now, account_id=account)
        playlist = body.get('playlist') or None
        verified_album = context if (context and body.get('videoVerified') is True
                                     and not body.get('bindingConflict')
                                     and (not playlist or playlist == context['playlist'])) else None
        new, first_live = self.history.record_play_event(account, key, video or None, now,
            playlist_ytm_id=playlist, like_status=status or None, verified_album=verified_album,
            source_timezone=body.get('timeZone'), context_choice=self.attributed(body, now, account, context),
            report_observation=True)
        if first_live and attribute:
            self.record_playback(body, now, account_id=account)
        if new:
            self.settings.set_setting('last_plays_sync_at', str(now))
            initial = rating['status'] if rating else status if status in {'LIKE', 'DISLIKE'} else None
            self.capture_start(account, video, playlist, initial, now,
                               album=bool(context), inline=inline_analytics)
        if status in {'LIKE', 'DISLIKE'} and video:
            if confirmed:
                self.reconcile_ratings({key})
            else:
                self.rating_observations(account, {video: status}, source='action', now=now)
        return new if attribute else first_live

    def _player(self, observation, capture_id, *, attribute):
        body, account, now = observation.body, observation.account_id, observation.at
        projected = player_projection(body)
        kind, video, playlist = projected['kind'], projected['video'], projected['playlist']
        position, duration, payload = projected['position'], projected['duration'], projected['payload']
        if capture_id is not None:
            payload['capture_id'] = capture_id
        event_id = self.events.record_player_event(account, kind, video, position, duration, playlist,
            json.dumps(payload) if payload else None, now, capture_id=capture_id)
        if attribute and projected['exit']:
            self.observe_exit(account, playlist, video, kind, position, duration, event_id, now)
        if projected['rating'] is not None:
            changed = self.confirmed_rating(account, video, projected['rating'], now,
                                            playlist=playlist, capture_id=f'player:{event_id}')
            self.refresh_ratings()
            if projected['rating'] == 'LIKE':
                if not attribute:
                    return changed
                if changed:
                    self.observe_like(account, playlist, video, event_id, payload.get('requestedAt'), now)
        return True
