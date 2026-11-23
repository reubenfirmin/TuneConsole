"""Clicked pool commands with explicit acquisition and publication capabilities."""
from dataclasses import dataclass
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.util.matching import identity_key


@dataclass(frozen=True)
class PoolOutcome:
    result: dict | None = None
    outcome: str | None = None
    error: str | None = None
    validation_code: str | None = None


class PoolBuilds:
    def __init__(self, *, client, populate, prepare_fresh, head, prepare_library,
                 refresh_candidates, refresh_home, now, logger):
        self.client, self.populate, self.prepare_fresh = client, populate, prepare_fresh
        self.head, self.prepare_library = head, prepare_library
        self.refresh_candidates, self.refresh_home = refresh_candidates, refresh_home
        self.now, self.logger = now, logger

    def fresh(self, parameters, progress):
        result = None
        try:
            self.populate(self.client(), self.now(), **parameters, on_progress=progress)
            progress('Preparing your fresh mix…')
            head = self.head('fresh')
            result = {'generation': head['id'] if head else None, 'requested_at': self.now()}
            waiting = self.prepare_fresh(self.now()) is None
            if waiting:
                progress('Your fresh mix is queued for the next music update.')
                self.refresh_candidates()
            self.refresh_home(force=True)
            return PoolOutcome(result=result, outcome='waiting' if waiting else None)
        except Exception as exc:
            self.logger.warning('Fresh pool build failed', exc_info=True)
            return PoolOutcome(result=result, outcome='failed', error=str(exc) or type(exc).__name__,
                validation_code=exc.code if isinstance(exc, ActionInputError) else None)

    def library(self, parameters, progress):
        try:
            progress('Finding matching songs in your library…')
            self.prepare_library(self.now(), parameters)
            self.refresh_home(force=True)
            return PoolOutcome()
        except Exception:
            self.logger.warning('Library pool build failed', exc_info=True)
            return PoolOutcome(error="Couldn't prepare this mix just now. Try again.")


def fresh_options(options, include, avoid):
    labels = {row['family']: row['label'] for row in options}
    families = sorted(set(include)) or [row['family'] for row in options if row['family'] not in avoid][:6]
    return labels, families


def fresh_candidate(row, key, owned, have, blocked, muted, genre, axes, include, avoid):
    if not row.get('title') or not row.get('artist') or not row.get('video_id'):
        return False
    family = axes[row.get('genre') or genre][0]
    return (key not in owned | have | blocked and row['artist'] not in muted
            and family not in avoid and (not include or family in include))


class FreshAcquisition:
    def __init__(self, query, discovery, model, genres, tracks, *, options, artists, songs, rebuild):
        self.query, self.discovery, self.model, self.genres, self.tracks = query, discovery, model, genres, tracks
        self.options, self.artists, self.songs, self.rebuild = options, artists, songs, rebuild

    def populate(self, client, now, include=(), avoid=(), progress=lambda message: None):
        if client is None:
            raise ActionInputError('fresh_account_required')
        labels, families = fresh_options(self.options(), include, avoid)
        if not families:
            raise ActionInputError('fresh_genres_required')
        owned, have = self.query.library_keys(), self.discovery.discovered_track_keys()
        blocked, muted = self.model.suppressed_keys('for_you', now), self.model.muted_artists()
        members = self.genres.family_members(family for family in families if family not in labels)
        added = 0
        for family in families:
            check_cancellable_work()
            genre = labels.get(family) or next(iter(members.get(family, ())), family.removeprefix('other:'))
            progress(f'Finding fresh {genre} songs…')
            for artist in self.artists(genre, limit=6):
                check_cancellable_work()
                if artist in muted:
                    continue
                rows, _ = self.songs(client, artist, 4)
                axes = self.genres.feature_axes(row.get('genre') or genre for row in rows)
                for row in rows:
                    if not row.get('video_id'):
                        continue
                    key = self.tracks.identity_key_for_video(row['video_id'])
                    if not fresh_candidate(row, key, owned, have, blocked, muted, genre, axes, include, avoid):
                        continue
                    key = self.discovery.upsert_discovered_track(identity_key(row['title'], row['artist']),
                        row['video_id'], row['title'], row['artist'], row.get('album'), row.get('thumbnail'),
                        row.get('genre') or genre, None, f'genre:{family}', now)
                    have.add(key)
                    added += 1
            progress(f'Found {added} new songs. Preparing your mix…')
        self.rebuild()
        return added


class LibraryPoolPreparation:
    def __init__(self, ledger, recommendations, *, vibe, scheduled, snapshot):
        self.ledger, self.recommendations = ledger, recommendations
        self.vibe, self.scheduled, self.snapshot = vibe, scheduled, snapshot

    def prepare(self, now, parameters):
        if self.vibe(now):
            return self.scheduled(now)
        with self.snapshot() as capture:
            tracks = capture.calculate(now, parameters)
            policies = capture.policies(now)
            tracks = policies.stamp(tracks, parameters['lane'])
            policy = policies.generation_policy()
            rows, bases = list(policies.rows.values()), list(policies.bases.values())
            plan = capture.recommendations.prepare_clicked_pool('home_pool:' + parameters['lane'],
                {'parameters': parameters, 'tracks': tracks}, now, policy=policy,
                input_revision=capture.serial(), pending_policies=rows)
            capture.release()
            self.ledger.record_prepared_policies(rows, bases)
            build = self.recommendations.claim(plan)
            self.recommendations.remember_clicked_pool_parameters(build.generation_id, parameters, now)
            self.recommendations.stage(plan, build)
            return capture.publish(lambda: self.recommendations.publish(build, now), validate_inputs=False)
