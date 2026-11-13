"""One application's Road Trip draft mutations and generation-scoped build jobs."""
import hashlib
import json
import logging
import random
import threading
from dataclasses import dataclass
from uuid import uuid4

from yt_playlist.core.activity import activity_for
from yt_playlist.rec import measurement, road_trip
from yt_playlist.rec import road_trip_candidates as candidates
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.util.matching import identity_key


@dataclass(frozen=True)
class _Build:
    recipe_id: int
    generation: str
    client: object


class RoadTripDrafts:
    def __init__(self, ctx, spawn):
        self.store, self.now = ctx.store, ctx.now_fn
        self.work, self.spawn = activity_for(ctx), spawn
        self._lock = threading.RLock()
        self._jobs = {}

    def _read(self, recipe_id):
        state = self.store.road_trip.get_road_trip_draft(recipe_id)
        return road_trip.normalized(state, self.store, self.now()) if state is not None else None

    def _replace(self, recipe_id, state, client):
        """A user change invalidates every acquisition based on the preceding state."""
        state['generation'] = uuid4().hex
        state['revision'] = uuid4().hex
        state['build_error'] = None
        self.store.road_trip.save_road_trip_draft(recipe_id, state, self.now())
        self._jobs.pop(recipe_id, None)
        job = None
        if state.get('building') and client is not None:
            job = _Build(recipe_id, state['generation'], client)
            self._jobs[recipe_id] = job
        return job

    def save_recipe(self, recipe_id, name, own_pct, artists, genres, target_minutes, *,
                    familiarity_pct, blacklist_genres, client):
        """Resolve a genre for any newly added artist, save the recipe, then refresh its draft."""
        artists, genres = list(artists), list(genres)
        previous = self.store.road_trip.get_road_trip_recipe(recipe_id) if recipe_id is not None else None
        previous_artists = {a.casefold() for a in (previous or {}).get("artists", [])}
        known_genres = {canonical_genre(g) for g in genres}
        for artist in artists:
            if artist.casefold() in previous_artists:
                continue
            genre = canonical_genre(candidates.artist_genre(self.store, artist))
            if genre and genre not in known_genres:
                genres.append(genre)
                known_genres.add(genre)
        with self._lock:
            recipe_id = self.store.road_trip.save_road_trip_recipe(
                recipe_id, name, own_pct, artists, genres, target_minutes, self.now(),
                familiarity_pct=familiarity_pct, blacklist_genres=blacklist_genres)
            state = self._read(recipe_id)
            job = None
            if state is not None:
                recipe = self.store.road_trip.get_road_trip_recipe(recipe_id)
                state = road_trip.apply_recipe(state, self.store, self.now(), recipe)
                job = self._replace(recipe_id, state, client)
        self._launch(job)
        return recipe_id

    def build(self, recipe_id, client):
        with self._lock:
            recipe = self.store.road_trip.get_road_trip_recipe(recipe_id)
            if recipe is None:
                return None
            previous = self.store.road_trip.get_road_trip_draft(recipe_id) or {}
            state = road_trip.start_draft(self.store, recipe, self.now(), random.randrange(1 << 30),
                                         previous.get('picks'))
            job = self._replace(recipe_id, state, client)
        self._launch(job)
        return state

    def tilt(self, recipe_id, party, axis, share, client):
        """Set the requested percentage and proportionally rebalance the rest of its slider group."""
        return self._mutate(recipe_id, client,
            lambda state: road_trip.set_share(state, party, axis, share, self.store, self.now()))

    def unpin(self, recipe_id, party, axis, client):
        """Reset this genre or year group to follow the available tracks again."""
        return self._mutate(recipe_id, client,
            lambda state: road_trip.clear_share(state, party, axis, self.store, self.now()))

    def reroll(self, recipe_id, index, client):
        """Cross out one slot; the recipe fills it back in with something else of the same side."""
        return self._mutate(recipe_id, client,
            lambda state: road_trip.reroll_slot(state, self.store, index, self.now()))

    def shuffle(self, recipe_id, client):
        """A different draw from the same pool: no network, new seed."""
        def change(state):
            state["seed"] = random.randrange(1 << 30)
            return road_trip.repick(state, self.store, self.now())
        return self._mutate(recipe_id, client, change)

    def _mutate(self, recipe_id, client, change):
        with self._lock:
            state = self._read(recipe_id)
            if state is None:
                return None
            state = change(state)
            job = self._replace(recipe_id, state, client)
        self._launch(job)
        return state

    def view(self, recipe_id=None):
        with self._lock:
            entry = (None if recipe_id is None else
                     {'recipe_id': recipe_id, 'state': self.store.road_trip.get_road_trip_draft(recipe_id)})
            if recipe_id is None:
                entry = self.store.road_trip.latest_road_trip_draft()
            if not entry or entry['state'] is None:
                return None
            rid = entry['recipe_id']
            state = road_trip.normalized(entry['state'], self.store, self.now())
            job = self._jobs.get(rid)
            if state.get('building') and (job is None or job.generation != state.get('generation')):
                # A process restart can leave a persisted build without a job.
                revision = state.get('revision')
                state = road_trip.finish_draft(state, self.store, self.now())
                state['revision'] = uuid4().hex
                self.store.road_trip.replace_road_trip_draft(rid, state, self.now(),
                                                           expected_revision=revision)
            return {'recipe_id': rid, 'state': state}

    def view_with_measurement(self, recipe_id=None):
        """The draft to show, recording one impression per distinct shown content.

        Progress polls of an unchanged draft are one offer, rather than new opportunities.
        """
        entry = self.view(recipe_id)
        if entry is None:
            return None
        state, rid = entry['state'], entry['recipe_id']
        tracks = road_trip.draft_tracks(state)
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
        request_id = f"road-trip:{rid}:{fingerprint}"
        if not self.store.rec_ledger.has_served_request(request_id):
            measurement.record_recommendations(self.store, "road_trip", [
                {**track, "key": self.store.tracks.identity_key_for_video(track["video_id"]) or
                                identity_key(track["title"], track["artist"]),
                 "out_of_corpus": track.get("source") == "theirs"} for track in tracks],
                self.now(), lane="draft", ranker="road-trip-mix", request_id=request_id,
                provenance={"recipe_id": rid, "seed": state.get("seed"),
                            "own_pct": state.get("own_pct"),
                            "familiarity_pct": state.get("familiarity_pct"),
                            "targets": state.get("targets"), "blacklist_genres": state.get("blacklist_genres")})
        return {"tracks": tracks, "recipe_id": rid, "state": state}

    def delete(self, recipe_id, *, recipe=False):
        with self._lock:
            self._jobs.pop(recipe_id, None)
            delete = (self.store.road_trip.delete_road_trip_recipe if recipe else
                      self.store.road_trip.delete_road_trip_draft)
            delete(recipe_id)

    def for_save(self, recipe_id):
        with self._lock:
            return self.store.road_trip.get_road_trip_recipe(recipe_id), self._read(recipe_id)

    def saved(self, recipe_id, source, playlist_id):
        job = None
        with self._lock:
            self.store.road_trip.set_road_trip_last_playlist(recipe_id, playlist_id)
            current = self.store.road_trip.get_road_trip_draft(recipe_id)
            if (current is not None and current.get('generation') == source.get('generation')
                    and current.get('revision') == source.get('revision')):
                # Persist the normalized selection that was actually saved.
                # The raw draft can still contain obsolete passenger picks.
                current = {**source, 'saved_playlist_id': playlist_id}
                previous_job = self._jobs.get(recipe_id)
                job = self._replace(recipe_id, current, previous_job.client if previous_job else None)
        self._launch(job)

    def _current(self, job):
        if self._jobs.get(job.recipe_id) is not job:
            return None
        state = self.store.road_trip.get_road_trip_draft(job.recipe_id)
        return state if state is not None and state.get('generation') == job.generation else None

    def _publish(self, job, state):
        with self._lock:
            if self._current(job) is None:
                return False
            revision = state['revision']
            replacement = {**state, 'revision': uuid4().hex}
            published = self.store.road_trip.replace_road_trip_draft(
                job.recipe_id, replacement, self.now(), expected_revision=revision)
            if published:
                state['revision'] = replacement['revision']
            return published

    def _launch(self, job):
        if job is None:
            return
        run = None
        try:
            run = self.work.wrap('Road Trip build', self._run, job)
            self.spawn(run)
        except BaseException:
            if run is not None:
                run.release()
            self._finish(job, 'Could not start the build.')
            raise

    def _run(self, job):
        error = None
        revision = None
        try:
            while True:
                with self._lock:
                    state = self._current(job)
                if state is None:
                    return
                revision = state['revision']
                if not state.get('pending'):
                    break
                # Only the detached state is changed during slow acquisition.
                # User edits can proceed and invalidate this exact revision.
                road_trip.add_other_input(state, self.store, job.client, state['pending'].pop(0))
                if not self._publish(job, state):
                    return
                revision = state['revision']
            if state.get('own_facts_left'):
                state['phase'] = 'mine'
                if not self._publish(job, state):
                    return
                revision = state['revision']
                road_trip.fill_own_facts(state, self.store, self.now())
                if self._publish(job, state):
                    revision = state['revision']
        except Exception as exc:
            logging.getLogger(__name__).exception('Road Trip acquisition failed for recipe %s generation %s',
                                                  job.recipe_id, job.generation)
            error = str(exc) or type(exc).__name__
            raise
        finally:
            self._finish(job, error, revision)

    def _finish(self, job, error, revision=None):
        with self._lock:
            try:
                state = self._current(job)
                if state is not None and (revision is None or state.get('revision') == revision):
                    if error:
                        state['build_error'] = error
                    state = road_trip.finish_draft(state, self.store, self.now())
                    self._publish(job, state)
            finally:
                # An old worker never clears the replacement job's ownership.
                if self._jobs.get(job.recipe_id) is job:
                    self._jobs.pop(job.recipe_id)
