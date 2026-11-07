"""Interpret listening captures before handing dated evidence to persistence."""
import datetime
import logging
import time
from collections import Counter, OrderedDict

from yt_playlist.core.history_policy import (
    EXACT_TOLERANCE_S, UNDATED_BUCKETS, parse_played_date as _parse_played_date,
    removable_source, repeats_live_event, play_subject,
)
from yt_playlist.core.listening import machine_timezone
from yt_playlist.core.listening_calendar import zone, day_at
from yt_playlist.repos.base import GENERATED_GROUP

logger = logging.getLogger(__name__)


class History:
    def __init__(self, repository, artists, maintenance, transient_playlists, guard, calendar):
        self.repository, self.artists, self.maintenance = repository, artists, maintenance
        self.transient_playlists, self.guard = transient_playlists, guard
        self.calendar = calendar
        self._bucket_warnings = OrderedDict()

    def _calendar(self, account_id, source_timezone=None):
        explicit = zone(source_timezone) is not None
        if source_timezone and not explicit:
            logger.warning('Ignoring invalid listening timezone %r', source_timezone)
        current = self.repository.calendar_facts(account_id)
        if current is None or (explicit and not current['confirmed']):
            name = source_timezone if explicit else machine_timezone()
            event_days = [(row['id'], day_at(row['played_at'], name), name)
                for row in self.repository.calendar_events(account_id)] if current is not None else None
            self.repository.set_calendar(account_id, name, confirmed=explicit, event_days=event_days)
            if current is not None:
                self.calendar.reconcile(account_id)
            return name
        return current['timezone']

    def record_history_plays(self, account_id, taken_at, items, *, source_timezone=None):
        with self.guard():
            calendar = self._calendar(account_id, source_timezone)
            source_zone = source_timezone if zone(source_timezone) else calendar
            parsed, skipped, observations, subjects, foreign = {}, Counter(), [], {}, set()
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
                if key not in subjects:
                    subjects[key] = self._subject(key, None)
                subject, recording = subjects[key]
                observations.append((key, subject, recording, day, day if source_zone == calendar else None,
                                     'independent' if bare else 'ytm_history'))
                if source_zone != calendar:
                    foreign.add((subject, day, source_zone))
            before = self.repository.day_count(account_id)
            self.repository.record_days(account_id, taken_at, observations, source_zone)
            self.calendar.reconcile(account_id, observations=foreign)
            result = max(0, self.repository.day_count(account_id) - before)
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

    def _subject(self, key, video):
        return play_subject(key, video, self.repository.subject_facts(key, video))

    def record_play_event(self, account_id, identity_key, video_id, played_at,
                          playlist_ytm_id=None, like_status=None, *, source_timezone=None,
                          context_choice=False):
        with self.guard():
            calendar = self._calendar(account_id, source_timezone)
            quarantined = self.repository.playlist_quarantined(account_id, playlist_ytm_id, GENERATED_GROUP)
            transient = (playlist_ytm_id or '').removeprefix('RDAMPL') in self.transient_playlists()
            subject, recording = self._subject(identity_key, video_id)
            with self.repository.transaction():
                row = self.repository.matching_event(account_id, subject, video_id, played_at, EXACT_TOLERANCE_S)
                if row is None:
                    latest = self.repository.latest_event(account_id)
                    if repeats_live_event(latest, subject, video_id, played_at):
                        row = latest
                generated = int(bool(quarantined or context_choice or transient))
                if row is None:
                    event = self.repository.append_event(account_id, identity_key, played_at, video_id=video_id,
                        playlist_ytm_id=playlist_ytm_id, like_status=like_status, is_generated=generated,
                        subject=subject, recording=recording)
                    timestamp = played_at
                else:
                    event, timestamp = row['id'], row['played_at']
                    self.repository.update_event(event, account_id, video_id, playlist_ytm_id, like_status, generated)
                self.repository.project_event(event, day_at(timestamp, calendar), calendar, 'live')
                self.repository.invalidate_first_play()
            self.calendar.reconcile(account_id, events={(subject, timestamp)})
            return row is None

    def import_listening_events(self, account_id, rows, *, source='takeout', source_timezone=None):
        with self.guard():
            calendar = self._calendar(account_id, source_timezone)
            before, added, affected = self.repository.day_count(account_id), 0, set()
            with self.repository.transaction():
                for key, video, timestamp in rows:
                    if not key:
                        continue
                    subject, recording = self._subject(key, video)
                    event, timestamp, new = self._import_event(account_id, key, subject, recording, video, timestamp, source, calendar)
                    added += new
                    affected.add((subject, timestamp))
                self.repository.invalidate_first_play()
            self.calendar.reconcile(account_id, events=affected)
            return max(0, self.repository.day_count(account_id) - before), added

    def _import_event(self, account, key, subject, recording, video, timestamp, source, calendar):
        row = self.repository.matching_event(account, subject, video, timestamp, EXACT_TOLERANCE_S)
        if row is None:
            event = self.repository.append_event(account, key, timestamp, video_id=video, subject=subject, recording=recording)
        else:
            event, timestamp = row['id'], row['played_at']
            self.repository.update_event(event, account, video, None, None, None)
        self.repository.project_event(event, day_at(timestamp, calendar), calendar, source)
        return event, timestamp, row is None

    def import_plays(self, account_id, plays, *, source='takeout', source_timezone=None):
        return self.import_listening_events(account_id, ((key, None, at) for key, at in plays),
            source=source, source_timezone=source_timezone)[0]

    def import_play_events(self, account_id, rows, source='takeout'):
        return self.import_listening_events(account_id, rows, source=source)[1]

    def import_spotify_listens(self, account_id, rows):
        with self.guard():
            calendar = self._calendar(account_id)
            artists, prepared = {}, []
            for uri, at, artist, key in rows:
                if not key and artist not in artists:
                    artists[artist] = self.artists.state_id(artist)
                prepared.append((uri, at, artists[artist] if not key else None, key))
            before, added, listens_added, affected = self.repository.day_count(account_id), 0, 0, set()
            with self.repository.transaction():
                for uri, at, artist, key in prepared:
                    receipt = self.repository.spotify_receipt(account_id, uri, at)
                    if receipt and (receipt['event_id'] is not None or not key):
                        continue
                    listens_added += receipt is None
                    event = None
                    if key:
                        subject, recording = self._subject(key, None)
                        event, timestamp, new = self._import_event(account_id, key, subject, recording, None, at, 'spotify_export', calendar)
                        added += new
                        affected.add((subject, timestamp))
                    self.repository.store_spotify_receipt(account_id, uri, at, event, artist)
                self.repository.invalidate_first_play()
            self.calendar.reconcile(account_id, events=affected)
            return max(0, self.repository.day_count(account_id) - before), added, listens_added

    def remove_listening_source(self, source, account_id=None):
        self.repository.remove_listening_source(removable_source(source), account_id)
        for account in self.repository.calendar_accounts(account_id):
            self.calendar.reconcile(account)

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
