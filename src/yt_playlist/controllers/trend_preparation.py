"""Capture recap inputs and publish independently of the calculation and web layers."""
from datetime import datetime
import json

from yt_playlist.rec import musical_character, recap_details, recap_story, trend_calculations, trend_statistics as stats
from yt_playlist.rec import vibe_intensity
from yt_playlist.rec.recap_metrics import month_bounds, shift_month


def day_counts(trends):
    return trends.ledger_day_counts() if trends.has_play_ledger() else trends.play_day_counts()


def review(source, months, now, counts=(), meta=None, *, artist_first=None):
    month = stats.last_complete_month(now)
    if not any(m['month'] == month for m in months):
        return None
    start, end = month_bounds(month)
    pstart, _ = month_bounds(shift_month(month, -1))
    trends = source.trends
    ledger = trends.has_play_ledger()
    this = trends.ledger_artist_plays(start, end, calendar=True) if ledger else trends.day_artist_plays(start, end)
    prev = trends.ledger_artist_plays(pstart, start, calendar=True) if ledger else trends.day_artist_plays(pstart, start)
    labels = trends.artist_labels()
    tracks = trends.ledger_track_plays(start, end, calendar=True) if ledger else trends.month_track_plays(start, end)
    top = max(tracks, key=lambda k: (tracks[k], k), default=None)
    artwork = {a: source.charts.artist_thumbnail(labels.get(a, a))
               for a in sorted(this, key=lambda a: (-this[a], a))[:3]}
    return trend_calculations.month_review(months, now, this=this, prev=prev, labels=labels,
        artist_first=trends.first_play_map('artist') if artist_first is None else artist_first,
        catalog=trends.catalog_artists(),
        track_plays=tracks, cards=trends.track_cards([top]) if top else {}, artwork=artwork,
        day_counts=counts, meta=meta)


def calculate(source, now):
    counts = day_counts(source.trends)
    meta = source.trends.track_meta()
    artist_first = source.trends.first_play_map('artist')
    catalog = source.trends.catalog_artists()
    axes = source.genres.feature_axes(row[1] for row in meta.values())
    months = stats.compute_months(counts, meta, artist_first, catalog, genre_axes=axes,
                                  family_adjacency=source.family_adjacency)
    return assemble(source, now, counts, meta, months, artist_first=artist_first)


def assemble(source, now, counts, meta, months, *, artist_first=None):
    current = review(source, months, now, counts, meta, artist_first=artist_first)
    story = None
    if current:
        month = current['month']
        since, until = month_bounds(month)
        detail_since, _ = month_bounds(shift_month(month, -6))
        details = source.recap.snapshot(detail_since, until)
        plays = source.trends.ledger_track_plays(since, until, calendar=True)
        profiles = musical_character.load(source, plays)
        audio = {}
        for key, profile in profiles.items():
            fits = ((profile or {}).get('vibes') or {}).get('intensity_fits') or {}
            observed = vibe_intensity.ensemble({name: fit for name, fit in fits.items()
                if isinstance(fit, dict) and fit.get('source') == 'audio_measurement'})
            if observed:
                audio[key] = {'energy': observed['score']}
        personality = recap_story.personality(next((m for m in months if m['month'] == month), None), meta,
            track_plays=plays, audio=audio, hours=source.trends.play_hours(since, until, calendar=True),
            genre_axes=source.genres.feature_axes(row[1] for row in meta.values()))
        story = recap_story.build_story(months, current, datetime.strptime(month + '-01', '%Y-%m-%d').strftime('%B'),
            personality_beat=personality, detail_beats=recap_details.screens(details, month))
    return {'built_at': now, 'story': story}


class TrendPreparation:
    def __init__(self, *, snapshot, publish):
        self.snapshot, self.publish = snapshot, publish

    def build(self, now):
        with self.snapshot() as source:
            payload = calculate(source, now)
            encoded = json.dumps(payload)
            if not source.is_current():
                return None
            if source.publish_ordered(lambda: self.publish(encoded, now)):
                return payload
            return None
