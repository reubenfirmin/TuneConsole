"""One application's Road Trip draft mutations and generation-scoped build jobs."""
import random
import threading
from dataclasses import dataclass
from uuid import uuid4

from yt_playlist.core.activity import activity_for
from yt_playlist.rec import road_trip


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

    def edit_recipe(self, save, client):
        # The caller resolves metadata before entering this local transaction.
        with self._lock:
            recipe_id = save()
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

    def mutate(self, recipe_id, change, client):
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
            if entry is None or entry['state'] is None:
                entry = self.store.road_trip.latest_road_trip_draft()
            if not entry:
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
