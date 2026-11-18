"""One application's Road Trip draft mutations and generation-scoped build jobs."""
import hashlib
import json
import logging
import random
import threading
from dataclasses import dataclass
from uuid import uuid4

from yt_playlist.rec import road_trip_state as road_trip
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.util.matching import identity_key


@dataclass(frozen=True)
class _Build:
    recipe_id: int
    generation: str
    client: object


class RoadTripDrafts:
    def __init__(self, repository, transitions, acquisition, *, now, work, spawn,
                 artist_genre, served, identity, record):
        self.repository, self.transitions, self.acquisition = repository, transitions, acquisition
        self.now, self.work, self.spawn = now, work, spawn
        self.artist_genre, self.served, self.identity, self.record = artist_genre, served, identity, record
        self._lock = threading.RLock()
        self._jobs = {}

    def _read(self, recipe_id):
        state = self.repository.get_road_trip_draft(recipe_id)
        return self.transitions.normalize(state, self.now()) if state is not None else None

    def _replace(self, recipe_id, state, client):
        """A user change invalidates every acquisition based on the preceding state."""
        state['generation'] = uuid4().hex
        state['revision'] = uuid4().hex
        state['build_error'] = None
        self.repository.save_road_trip_draft(recipe_id, state, self.now())
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
        previous = self.repository.get_road_trip_recipe(recipe_id) if recipe_id is not None else None
        previous_artists = {a.casefold() for a in (previous or {}).get("artists", [])}
        known_genres = {canonical_genre(g) for g in genres}
        for artist in artists:
            if artist.casefold() in previous_artists:
                continue
            genre = canonical_genre(self.artist_genre(artist))
            if genre and genre not in known_genres:
                genres.append(genre)
                known_genres.add(genre)
        with self._lock:
            recipe_id = self.repository.save_road_trip_recipe(
                recipe_id, name, own_pct, artists, genres, target_minutes, self.now(),
                familiarity_pct=familiarity_pct, blacklist_genres=blacklist_genres)
            state = self._read(recipe_id)
            job = None
            if state is not None:
                recipe = self.repository.get_road_trip_recipe(recipe_id)
                state = self.transitions.apply_recipe(state, recipe, self.now())
                job = self._replace(recipe_id, state, client)
        self._launch(job)
        return recipe_id

    def build(self, recipe_id, client):
        with self._lock:
            recipe = self.repository.get_road_trip_recipe(recipe_id)
            if recipe is None:
                return None
            previous = self.repository.get_road_trip_draft(recipe_id) or {}
            state = self.transitions.start(recipe, self.now(), random.randrange(1 << 30),
                                         previous.get('picks'))
            job = self._replace(recipe_id, state, client)
        self._launch(job)
        return state

    def tilt(self, recipe_id, party, axis, share, client):
        """Set the requested percentage and proportionally rebalance the rest of its slider group."""
        return self._mutate(recipe_id, client,
            lambda state: self.transitions.tilt(state, party, axis, share, self.now()))

    def unpin(self, recipe_id, party, axis, client):
        """Reset this genre or year group to follow the available tracks again."""
        return self._mutate(recipe_id, client,
            lambda state: self.transitions.unpin(state, party, axis, self.now()))

    def reroll(self, recipe_id, index, client):
        """Cross out one slot; the recipe fills it back in with something else of the same side."""
        return self._mutate(recipe_id, client,
            lambda state: self.transitions.reroll(state, index, self.now()))

    def shuffle(self, recipe_id, client):
        """A different draw from the same pool: no network, new seed."""
        def change(state):
            state["seed"] = random.randrange(1 << 30)
            return self.transitions.repick(state, self.now())
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
                     {'recipe_id': recipe_id, 'state': self.repository.get_road_trip_draft(recipe_id)})
            if recipe_id is None:
                entry = self.repository.latest_road_trip_draft()
            if not entry or entry['state'] is None:
                return None
            rid = entry['recipe_id']
            state = self.transitions.normalize(entry['state'], self.now())
            job = self._jobs.get(rid)
            if state.get('building') and (job is None or job.generation != state.get('generation')):
                # A process restart can leave a persisted build without a job.
                revision = state.get('revision')
                state = self.transitions.finish(state, self.now())
                state['revision'] = uuid4().hex
                self.repository.replace_road_trip_draft(rid, state, self.now(),
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
        if not self.served(request_id):
            self.record("road_trip", [
                {**track, "key": self.identity(track["video_id"]) or
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
            delete = (self.repository.delete_road_trip_recipe if recipe else
                      self.repository.delete_road_trip_draft)
            delete(recipe_id)

    def for_save(self, recipe_id):
        with self._lock:
            return self.repository.get_road_trip_recipe(recipe_id), self._read(recipe_id)

    def saved(self, recipe_id, source, playlist_id):
        job = None
        with self._lock:
            self.repository.set_road_trip_last_playlist(recipe_id, playlist_id)
            current = self.repository.get_road_trip_draft(recipe_id)
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
        state = self.repository.get_road_trip_draft(job.recipe_id)
        return state if state is not None and state.get('generation') == job.generation else None

    def _publish(self, job, state):
        with self._lock:
            if self._current(job) is None:
                return False
            revision = state['revision']
            replacement = {**state, 'revision': uuid4().hex}
            published = self.repository.replace_road_trip_draft(
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
                self.acquisition.add(state, job.client, state['pending'].pop(0))
                if not self._publish(job, state):
                    return
                revision = state['revision']
            if state.get('own_facts_left'):
                state['phase'] = 'mine'
                if not self._publish(job, state):
                    return
                revision = state['revision']
                self.acquisition.fill(state, self.now())
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
                    state = self.transitions.finish(state, self.now())
                    self._publish(job, state)
            finally:
                # An old worker never clears the replacement job's ownership.
                if self._jobs.get(job.recipe_id) is job:
                    self._jobs.pop(job.recipe_id)
