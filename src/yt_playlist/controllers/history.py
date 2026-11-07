"""Interpret listening captures before handing dated evidence to persistence."""
import datetime
import logging
import time
from collections import Counter, OrderedDict

from yt_playlist.core.history_policy import (
    EXACT_TOLERANCE_S, LIVE_DEDUP_S, UNDATED_BUCKETS, parse_played_date as _parse_played_date,
    removable_source,
)
from yt_playlist.core.listening import machine_timezone
from yt_playlist.core.listening_calendar import zone
from yt_playlist.repos.base import GENERATED_GROUP

logger = logging.getLogger(__name__)


class History:
    def __init__(self, repository, artists, maintenance, transient_playlists, guard):
        self.repository, self.artists, self.maintenance = repository, artists, maintenance
        self.transient_playlists, self.guard = transient_playlists, guard
        self._bucket_warnings = OrderedDict()

    def _calendar(self, account_id, source_timezone=None):
        explicit = zone(source_timezone) is not None
        if source_timezone and not explicit:
            logger.warning('Ignoring invalid listening timezone %r', source_timezone)
        current = self.repository.calendar_facts(account_id)
        if current is None or (explicit and not current['confirmed']):
            name = source_timezone if explicit else machine_timezone()
            self.repository.set_calendar(account_id, name, confirmed=explicit, reproject=current is not None)
            return name
        return current['timezone']

    def record_history_plays(self, account_id, taken_at, items, *, source_timezone=None):
        with self.guard():
            calendar = self._calendar(account_id, source_timezone)
            source_zone = source_timezone if zone(source_timezone) else calendar
            parsed, skipped, observations = {}, Counter(), []
            for item in items:
                bare = isinstance(item, str)
                key, played = (item, 'today') if bare else item
                if not key:
                    continue
                bucket = (played or '').strip().lower()
                if bucket not in parsed:
                    parsed[bucket] = _parse_played_date(played, taken_at, source_zone)
                stamp = parsed[bucket]
                if stamp is None:
                    skipped[bucket] += 1
                    continue
                day = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).date().isoformat()
                observations.append((key, day, 'independent' if bare else 'ytm_history'))
            result = self.repository.record_days(account_id, taken_at, observations, source_zone)
            self._log_undated_buckets(account_id, skipped)
            return result

    def _log_undated_buckets(self, account_id, skipped):
        if not skipped:
            return
        unexpected = Counter({bucket: count for bucket, count in skipped.items()
                              if bucket and bucket not in UNDATED_BUCKETS})
        logger.info('history sync: identity=%s skipped_undated=%d expected=%s missing=%d unexpected=%d',
                    account_id, sum(skipped.values()),
                    {bucket: skipped[bucket] for bucket in sorted(UNDATED_BUCKETS) if skipped[bucket]},
                    skipped[''], sum(unexpected.values()))
        logger.debug('history undated buckets: identity=%s counts=%s', account_id, dict(skipped))
        now, report = time.monotonic(), Counter()
        for bucket, count in unexpected.items():
            last = self._bucket_warnings.get(bucket)
            if last is None or now - last >= 900:
                report[bucket] = count
                self._bucket_warnings[bucket] = now
            if bucket in self._bucket_warnings:
                self._bucket_warnings.move_to_end(bucket)
            while len(self._bucket_warnings) > 128:
                self._bucket_warnings.popitem(last=False)
        if report:
            sample = {bucket[:120]: count for bucket, count in report.most_common(8)}
            logger.warning('history: unexpected played buckets: identity=%s rows=%d buckets=%s '
                           'other_categories=%d; skipped without inventing play dates',
                           account_id, sum(report.values()), sample, max(0, len(report) - 8))

    def record_play_event(self, account_id, identity_key, video_id, played_at,
                          playlist_ytm_id=None, like_status=None, *, source_timezone=None,
                          context_choice=False):
        with self.guard():
            self._calendar(account_id, source_timezone)
            quarantined = self.repository.playlist_quarantined(account_id, playlist_ytm_id, GENERATED_GROUP)
            transient = (playlist_ytm_id or '').removeprefix('RDAMPL') in self.transient_playlists()
            return self.repository.record_live(account_id, identity_key, video_id, played_at,
                playlist_ytm_id, like_status, is_generated=int(bool(quarantined or context_choice or transient)),
                exact_tolerance=EXACT_TOLERANCE_S, live_window=LIVE_DEDUP_S)

    def import_listening_events(self, account_id, rows, *, source='takeout', source_timezone=None):
        with self.guard():
            self._calendar(account_id, source_timezone)
            return self.repository.import_events(account_id, rows, source=source, exact_tolerance=EXACT_TOLERANCE_S)

    def import_plays(self, account_id, plays, *, source='takeout', source_timezone=None):
        return self.import_listening_events(account_id, ((key, None, at) for key, at in plays),
            source=source, source_timezone=source_timezone)[0]

    def import_play_events(self, account_id, rows, source='takeout'):
        return self.import_listening_events(account_id, rows, source=source)[1]

    def import_spotify_listens(self, account_id, rows):
        with self.guard():
            self._calendar(account_id)
            artists, prepared = {}, []
            for uri, at, artist, key in rows:
                if not key and artist not in artists:
                    artists[artist] = self.artists.state_id(artist)
                prepared.append((uri, at, artists[artist] if not key else None, key))
            return self.repository.import_spotify_receipts(account_id, prepared,
                source='spotify_export', exact_tolerance=EXACT_TOLERANCE_S)

    def remove_listening_source(self, source, account_id=None):
        return self.repository.remove_listening_source(removable_source(source), account_id)

    def repair_verified_imports(self):
        return self.maintenance.repair_verified_imports()

    def remove_spotify_history(self):
        return self.maintenance.remove_spotify_history()

    def reset_play_history(self, account_id=None):
        return self.repository.reset_play_history(account_id)

    def history_keys_before(self, ts):
        return self.repository.history_keys_before(ts)

    def history_keys_between(self, start_ts, end_ts):
        return self.repository.history_keys_between(start_ts, end_ts)

    def play_counts_before(self, ts):
        return self.repository.play_counts_before(ts)

    def recent_plays_with_ts(self, limit=None, exclude_list_ids=None):
        return self.repository.recent_plays_with_ts(limit, exclude_list_ids)

    def last_plays_for(self, keys):
        return self.repository.last_plays_for(keys)

    def listening_comparison_coverage(self, since, until, *, exclude_list_ids=()):
        return self.repository.listening_comparison_coverage(since, until, exclude_list_ids=exclude_list_ids)

    def play_events_since(self, since_ts, exclude_list_ids=None, *, taste_only=False):
        return self.repository.play_events_since(since_ts, exclude_list_ids, taste_only=taste_only)

    def record_takeout_import(self, fingerprint, account_id, imported_at, parsed, unparsed_dates):
        return self.repository.record_takeout_import(fingerprint, account_id, imported_at,
            parsed[0]['ts'] if parsed else None, parsed[-1]['ts'] if parsed else None, unparsed_dates)

    def listening_day_counts(self, account_id=None):
        return self.repository.listening_day_counts(account_id)

    def event_play_counts(self, account_id=None):
        return self.repository.event_play_counts(account_id)

    def spotify_unmatched_artists(self, account_id):
        return self.repository.spotify_unmatched_artists(account_id)

    def spotify_history_stats(self):
        return self.repository.spotify_history_stats()
