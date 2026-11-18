"""Bounded group capture, discovery and guarded publication through explicit ports."""
import json
from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.rec import group_discovery, group_names, group_similarity, group_versions, musical_profiles
from yt_playlist.repos import group_publication

CHECKPOINT = 'rec_group_shadow_checkpoint'
# The serial admits work and labels its capture; musical inputs retain their
# own guards. Operational source timestamps must not discard staged progress.
BOOKKEEPING = frozenset({'rec_group_clock'})
PROGRESS = 'rec_group_shadow_progress'


def report(settings, phase, now, *, reason=None):
    from yt_playlist.core.group_progress import PHASES
    if phase not in PHASES:
        raise ValueError('Unsupported grouping progress phase')
    settings.set_setting(PROGRESS, json.dumps({'phase': phase, 'at': now, 'reason': reason}))


class GroupPreparation:
    def __init__(self, groups, content, settings, diagnostics, *, capture, report, policy, budget):
        self.groups, self.content, self.settings, self.diagnostics = groups, content, settings, diagnostics
        self.capture, self.report, self.policy, self.budget = capture, report, policy, budget
        self.retry_at = 0
        self._pending = None

    def due(self, now):
        if now < self.retry_at or self.settings.get_setting('rec_group_shadow') == '0':
            return False
        if self._pending is not None:
            return True
        if self.groups.expired_builds():
            return True
        previous = self.groups.head_checkpoint()
        if previous is None:
            return bool(self.content.changed_since(-1))
        if previous['policy'] != musical_profiles.canonical(self.policy.manifest()):
            return True
        checkpoint = json.loads(self.settings.get_setting(CHECKPOINT) or '{}')
        serial = checkpoint.get('serial', previous['serial']) if checkpoint.get('generation') == previous['generation'] else previous['serial']
        return self.groups.serial() != serial

    def step(self, now):
        if not self.due(now):
            return False
        self.retry_at = now + self.budget.interval
        self.report('preparing', now)
        try:
            # Capture and discovery have no recoverable intermediate result.
            # Finish that admitted plan; staging already commits resumable
            # prefixes and retains _pending if an output quota makes it yield.
            if self._pending is not None:
                snapshot, prepared = self._pending
                if (self.groups.head_generation() == prepared['generation']
                        or snapshot.is_current(allow_stale_tables=self.groups.staged_tables | BOOKKEEPING)):
                    return self._publish_pending(now)
                self._pending = None
                self.groups.abandon_staged(prepared)
            return self._prepare(now)
        except group_publication.BuildUnavailable:
            self.report('deferred', now, reason='build-owned')
            self.diagnostics.record('deferred', phase='rec: adaptive grouping',
                reasons=('grouping-build-owned',))
        except WorkBudgetExceeded:
            self.report('deferred', now, reason='work-budget')
            self.diagnostics.record('deferred', phase='rec: adaptive grouping',
                reasons=('grouping-work-budget',))
        return False

    def _prepare(self, now):
        with self.capture() as (snapshot, groups, genres, metadata_query):
            serial = groups.serial()
            groups.corpus_size(self.budget.max_rows)
            # Grouping is independent of the collaborative/content basis. Only the library
            # contributes interests; discovered candidates do not manufacture user interests.
            recordings, after, axes = [], '', {}
            while page := groups.corpus_page(after, max_bytes=self.budget.input_bytes):
                missing = {payload['library'].get('genre') for _key, payload, _profile in page
                           if payload['library'] is not None} - axes.keys()
                if missing:
                    matched = genres.feature_axes(missing)
                    axes.update((genre, matched.get(genre, ('', None))) for genre in missing)
                for key, payload, profile in page:
                    check_stopping()
                    if payload['library'] is not None:
                        recordings.append(group_similarity.recording(key, payload['library'], profile, genre_axes=axes))
                after = page[-1][0]
            fingerprint = group_versions.fingerprint([self.policy.manifest(), [
                [r.key, r.genre, r.family, r.year, r.values, r.support, r.profile_version] for r in recordings]])
            previous = groups.head(members=True)
            # Legacy offers carry their own captured evidence. Reimporting
            # mutable retired modes on every build recreates collected history
            # and pins obsolete centroids. Existing legacy versions stay readable.
            legacy_modes, legacy_versions = [], ()
            parent = previous['generation'] if previous else None
            resumable = groups.resumable_build(fingerprint, serial,
                musical_profiles.canonical(self.policy.manifest()), parent)
            if previous and previous['fingerprint'] == fingerprint and resumable is None:
                # Operational stamps and unchanged legacy rewrites are not new musical input.
                if snapshot.publish(lambda: self.groups.checkpoint_preparation(previous['generation'], serial, now),
                                    allow_stale_tables=BOOKKEEPING):
                    self.report('ready', now)
                    # Expired unrelated builds and old graph caches do not get
                    # to consume every turn before a new plan can make progress.
                    snapshot.release_reader()
                    while self.groups.cleanup_staged():
                        check_stopping()
                else:
                    self.report('deferred', now, reason='inputs-changed')
                return False
            result = group_discovery.discover(recordings, policy=self.policy)
            metadata = {}
            for offset in range(0, len(recordings), 512):
                check_stopping()
                metadata.update(metadata_query([r.key for r in recordings[offset:offset + 512]]))
            generation = group_versions.fingerprint(['resumable-staging-1', fingerprint, serial, parent,
                group_names.POLICY, [group_versions.legacy_definition(mode)[0] for mode in legacy_modes], metadata])
            staged_at = groups.staged_at(generation)
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
        if self.groups.head_generation() == prepared['generation']:
            # A prior attempt may have committed the head before its checkpoint
            # failed. Finish that acknowledgement without staging it again.
            self.groups.checkpoint_preparation(prepared['generation'], prepared['serial'], now)
            published = True
        else:
            owner = self.groups.stage(prepared)
            def publish():
                self.groups.finish_preparation(prepared['generation'], owner,
                    prepared['serial'], now)
            published = snapshot.publish_ordered(publish,
                allow_stale_tables=self.groups.staged_tables | BOOKKEEPING)
        if published:
            self._pending = None
            self.report('ready', now)
            self.diagnostics.record('published', phase='rec: adaptive grouping',
                reasons=('shadow-generation',))
            return True
        self._pending = None
        self.groups.abandon_staged(prepared)
        self.report('deferred', now, reason='inputs-changed')
        return False
