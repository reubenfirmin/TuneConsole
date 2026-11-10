"""Legacy recommendation admission and file publication during offline import."""
from yt_playlist.core import recommendation_outputs, recommendation_plan, recommendation_pools
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.recommendation_files import RecommendationFiles
from yt_playlist.repos.recommendation_import import RecommendationImportRepo


def preflight(conn, plan):
    return RecommendationImport(RecommendationImportRepo(conn), None).preflight(plan)


def import_plan(conn, plan, published_at, *, before_publish=None):
    repository = RecommendationImportRepo(conn)
    path, memory_key = repository.file_location()
    files = RecommendationFiles(recommendation_outputs.root_for_database(path, memory_key))
    return RecommendationImport(repository, files).install(plan, published_at, before_publish=before_publish)


class RecommendationImport:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def preflight(self, plan):
        """Resolve every policy and canonical owner before any file is written."""
        digest = recommendation_plan.validate(plan)
        if self.repository.generation_exists(plan.external_key):
            raise ValueError('Recommendation import generation already exists')
        distinct = {slot.pool.content_digest: slot.pool for slot in plan.slots}
        families = self.repository.policy_families(list(dict.fromkeys(
            [plan.policy_version, *(pool.policy_version for pool in distinct.values())])))
        family = families.get(plan.policy_version)
        if family is None:
            raise ValueError('Recommendation import requires its recorded policy')
        if plan.group_generation_id is not None and not self.repository.published_group(plan.group_generation_id):
            raise ValueError('Recommendation import requires its published group generation')
        for prepared in distinct.values():
            check_cancellable_work()
            if families.get(prepared.policy_version) != family:
                raise ValueError('Recommendation import pool has incompatible policy inputs')
            if prepared.group_version_id is not None and not self.repository.group_contains(
                    plan.group_generation_id, prepared.group_version_id):
                raise ValueError('Recommendation import pool has incompatible group inputs')
            existing = self.repository.pool_owner(prepared.content_digest)
            if existing:
                if not existing['complete'] or existing['retiring']:
                    raise ValueError('Recommendation import cannot reuse an unfinished or retiring pool')
                if not self.repository.pool_matches(existing['id'], prepared):
                    raise ValueError('Recommendation pool digest collision or incomplete membership')
            for start in range(0, len(prepared.candidates), recommendation_plan.BATCH_ROWS):
                identities = [candidate.recording_id
                    for candidate in prepared.candidates[start:start + recommendation_plan.BATCH_ROWS]
                    if isinstance(candidate, recommendation_pools.Candidate)]
                if identities and self.repository.known_recordings(identities) != set(identities):
                    raise ValueError('Recommendation import has an unresolved canonical recording')
        return digest

    def install(self, plan, published_at, *, before_publish=None):
        """Use the caller's isolated import transaction and retain its source on failure."""
        if not self.repository.in_transaction() or not recommendation_plan.finite(published_at):
            raise RuntimeError('Recommendation import requires an outer transaction and finite time')
        digest = self.preflight(plan)
        build = self.repository.create(plan, digest)
        installed = {}
        for slot in plan.slots:
            check_cancellable_work()
            prepared = slot.pool
            if prepared.content_digest not in installed:
                existing = self.repository.pool_owner(prepared.content_digest)
                identity = existing['id'] if existing else self.repository.create_pool(prepared)
                if existing is None:
                    for start in range(0, len(prepared.candidates), recommendation_plan.BATCH_ROWS):
                        check_cancellable_work()
                        self.repository.append_pool(identity,
                            prepared.candidates[start:start + recommendation_plan.BATCH_ROWS], start)
                    if not self.repository.pool_matches(identity, prepared):
                        raise ValueError('Recommendation pool digest collision or incomplete membership')
                    self.repository.seal_pool(identity)
                installed[prepared.content_digest] = identity
            self.repository.attach_pool(build.generation_id, slot, installed[prepared.content_digest])
        for output in plan.outputs:
            check_cancellable_work()
            file = self.files.publish(output.encoded)
            self.repository.record_output(plan, build.generation_id, output, file)
        self.repository.ready(plan, build.generation_id)
        if before_publish is not None:
            before_publish(build.generation_id)
        self.repository.finish(build, published_at)
        return build.generation_id
