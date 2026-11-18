"""Bounded analytics acquisition and independent, guarded publication."""
import json

from yt_playlist.rec import listening_calculations as calculations
from yt_playlist.rec.listening_editions import select
from yt_playlist.rec.listening_insights import candidates


def calculate(analytics, playlists, settings, now):
    facts = calculations.ListeningInputs(names=analytics.artist_names(), catalog=analytics.catalog_rows(),
        radio_ids=playlists.radio_playlist_ids(), timezone=settings.get_setting('listening_timezone'),
        mixes=analytics.mix_playlist_ids(), videos=analytics.video_keys(), player=analytics.player(now),
        observations=analytics.observations(now), plays=analytics.plays(now),
        rating_actions=analytics.rating_actions(now), feedback=analytics.feedback(now))
    return calculations.calculate(facts, now)


class ListeningPreparation:
    def __init__(self, *, snapshot, prepare, publish):
        self.snapshot, self.prepare, self.publish = snapshot, prepare, publish

    def build(self, now):
        with self.snapshot() as source:
            payload = calculate(source.analytics, source.playlists, source.settings, now)
            edition = select(payload, now,
                previous=source.analytics.prepared('listening_edition') or {},
                views=source.analytics.impressions(), pool=candidates(payload))
            chunks, manifest = calculations.partition(payload)
            plan = self.prepare(chunks,
                json.dumps(manifest, separators=(',', ':'), allow_nan=False),
                json.dumps(edition, separators=(',', ':'), allow_nan=False), now,
                input_revision=source.revision)
            if source.publish(lambda: self.publish(plan, payload['timezone'])):
                return payload
        return None
