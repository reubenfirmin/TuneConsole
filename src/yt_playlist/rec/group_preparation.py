"""Coalesced shadow grouping on the existing worker, with bounded last-good publication."""
from dataclasses import dataclass
import json
from yt_playlist.core.activity import work_budget, WorkBudgetExceeded, check_stopping
from yt_playlist.rec import group_discovery, group_names, group_similarity, group_versions, musical_profiles
from yt_playlist.repos import group_publication

CHECKPOINT = 'rec_group_shadow_checkpoint'
PROGRESS = 'rec_group_shadow_progress'


def status(store):
    return json.loads(store.settings.get_setting(PROGRESS) or '{}')


def report(store, phase, now, *, reason=None):
    store.settings.set_setting(PROGRESS, json.dumps({'phase': phase, 'at': now, 'reason': reason}))


@dataclass(frozen=True)
class Budget:
    seconds: float = 10
    interval: float = 60
    max_rows: int = 16384
    input_bytes: int = 8 * 1024 * 1024


class Preparation:
    def __init__(self, store, *, policy=group_discovery.Policy(), budget=Budget()):
        self.store, self.policy, self.budget = store, policy, budget
        self.retry_at = 0
        self.rebuild_requested = False
        self._pending = None

    def request_rebuild(self):
        self.rebuild_requested = True
        self.retry_at = 0

    def due(self, now):
        if now < self.retry_at or self.store.settings.get_setting('rec_group_shadow') == '0':
            return False
        if self._pending is not None:
            return True
        if self.store.taste_groups.expired_builds():
            return True
        previous = self.store.taste_groups.head_checkpoint()
        if self.rebuild_requested or previous is None:
            return bool(self.store.prepared_content.changed_since(-1))
        if previous['policy'] != musical_profiles.canonical(self.policy.manifest()):
            return True
        checkpoint = json.loads(self.store.settings.get_setting(CHECKPOINT) or '{}')
        serial = checkpoint.get('serial', previous['serial']) if checkpoint.get('generation') == previous['generation'] else previous['serial']
        return self.store.taste_groups.serial() != serial

    def step(self, now):
        if not self.due(now):
            return False
        self.retry_at = now + self.budget.interval
        report(self.store, 'preparing', now)
        try:
            with work_budget(self.budget.seconds):
                if self._pending is not None:
                    snapshot, prepared = self._pending
                    if snapshot.publish(lambda: None, allow_stale_tables=group_publication.STAGED_TABLES):
                        return self._publish_pending(now)
                    self._pending = None
                    group_publication.abandon(self.store.taste_groups, prepared)
                return self._prepare(now)
        except group_publication.BuildUnavailable:
            report(self.store, 'deferred', now, reason='build-owned')
            self.store.maintenance_diagnostics.record('deferred', phase='rec: adaptive grouping',
                reasons=('grouping-build-owned',))
        except WorkBudgetExceeded:
            report(self.store, 'deferred', now, reason='work-budget')
            self.store.maintenance_diagnostics.record('deferred', phase='rec: adaptive grouping',
                reasons=('grouping-work-budget',))
        return False

    def _prepare(self, now):
        with self.store.read_snapshot() as snapshot:
            reader = snapshot.store
            serial = reader.taste_groups.serial()
            reader.taste_groups.corpus_size(self.budget.max_rows)
            # Grouping is independent of the collaborative/content basis. Only the library
            # contributes interests; discovered candidates do not manufacture user interests.
            recordings, after = [], ''
            while page := reader.taste_groups.corpus_page(after, max_bytes=self.budget.input_bytes):
                for key, payload, profile in page:
                    check_stopping()
                    if payload['library'] is not None:
                        recordings.append(group_similarity.recording(key, payload['library'], profile))
                after = page[-1][0]
            fingerprint = group_versions.fingerprint([self.policy.manifest(), [
                [r.key, r.genre, r.year, r.values, r.support, r.profile_version] for r in recordings]])
            previous = reader.taste_groups.head(members=True)
            legacy_modes = reader.modes.list_modes(active_only=False)
            legacy_versions = reader.taste_groups.legacy_versions()
            reader.settings.get_setting(CHECKPOINT)
            parent = previous['generation'] if previous else None
            resumable = reader.taste_groups.resumable_build(fingerprint, serial,
                musical_profiles.canonical(self.policy.manifest()), parent)
            if (previous and previous['fingerprint'] == fingerprint and not self.rebuild_requested and
                    resumable is None and
                    all(group_versions.legacy_definition(mode)[0] in legacy_versions for mode in legacy_modes)):
                # Operational stamps and unchanged legacy rewrites are not new musical input.
                checkpoint = json.dumps({'generation': previous['generation'], 'serial': serial, 'at': now})
                if snapshot.publish(lambda: self.store.settings.publish_values({CHECKPOINT: checkpoint})):
                    report(self.store, 'ready', now)
                    # Expired unrelated builds and old graph caches do not get
                    # to consume every turn before a new plan can make progress.
                    reader.conn.rollback()
                    while self.store.taste_groups.cleanup_staged():
                        check_stopping()
                else:
                    report(self.store, 'deferred', now, reason='inputs-changed')
                return False
            result = group_discovery.discover(recordings, policy=self.policy)
            metadata = {}
            for offset in range(0, len(recordings), 512):
                check_stopping()
                metadata.update(reader.modes.meta_for([r.key for r in recordings[offset:offset + 512]]))
            generation = group_versions.fingerprint(['resumable-staging-1', fingerprint, serial, parent,
                group_names.POLICY, [group_versions.legacy_definition(mode)[0] for mode in legacy_modes], metadata])
            staged_at = reader.taste_groups.staged_at(generation)
            prepared = group_versions.prepare(result, recordings, previous,
                legacy_modes, now=staged_at if staged_at is not None else now, serial=serial,
                input_fingerprint=fingerprint, policy=self.policy, metadata=metadata,
                legacy_versions=legacy_versions, generation=generation)
            prepared['_stage_parent'] = parent
            check_stopping()
            # Keep the compact admitted plan and its original revision guard
            # across turns, with no open reader or pinned WAL snapshot. Restart
            # reconstructs the same plan and verifies its retained output digest.
            self._pending = snapshot, prepared
            snapshot.release_reader()
            return self._publish_pending(now)

    def _publish_pending(self, now):
        snapshot, prepared = self._pending
        owner = self.store.taste_groups.stage(prepared)
        if snapshot.publish(lambda: self.store.taste_groups.finish_staged(prepared['generation'], owner),
                            allow_stale_tables=group_publication.STAGED_TABLES):
            self._pending = None
            self.rebuild_requested = False
            report(self.store, 'ready', now)
            self.store.maintenance_diagnostics.record('published', phase='rec: adaptive grouping',
                reasons=('shadow-generation',))
            return True
        self._pending = None
        group_publication.abandon(self.store.taste_groups, prepared)
        report(self.store, 'deferred', now, reason='inputs-changed')
        return False
