"""Prepare group files before committing their bounded SQL ownership."""
from itertools import islice

from yt_playlist.core import group_content as content, group_plan, group_storage
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.group_publication import _batches, _BuildBeingCleaned


class GroupPublication:
    def __init__(self, repository, files, policies):
        self.repository, self.files = repository, files
        self.policies = policies

    def stage(self, prepared):
        self.repository.assert_idle()
        identities = self.repository.recording_ids(group_plan.recording_keys(prepared))
        inputs, plans, policy, metadata = group_plan.prepare(prepared, identities)
        while (conflict := self.repository.conflicting_build(prepared)) is not None:
            while self.repository.cleanup_builds(generation=conflict):
                check_cancellable_work()
        with self.files.retained():
            version = group_storage.policy_version(prepared['policy'])
            while True:
                try:
                    metadata_reference = self.repository.generation_reference(prepared, version)
                    if metadata_reference is None:
                        self.policies.publish([(version, canonical(policy).decode(), prepared['at'])])
                        metadata_reference, = self.files.publish([('generation', metadata)])
                    generation, owner, ready = self.repository.claim_generation(prepared, version, metadata_reference)
                    break
                except _BuildBeingCleaned:
                    while self.repository.cleanup_builds(generation=prepared['generation']):
                        check_cancellable_work()
            if ready:
                return owner
            self.repository.install_identities(prepared, generation, owner)
            self._groups(prepared, generation, owner, plans, identities, inputs)
            self.repository.mark_ready(generation, owner, len(plans))
            return owner

    def _groups(self, prepared, generation, owner, plans, identities, inputs):
        stages = self.repository.stages(generation, owner)
        positions = {row[0]: row for row in plans}
        for batch in _batches((row[0], row[1]['payload']) for row in plans):
            pending = [positions[position] for position, _ in batch
                       if positions[position][1]['group_id'] not in stages]
            if pending:
                files = self.files.publish(('definition', row[2]) for row in pending)
                stages.update(self.repository.install_definitions(prepared, generation, owner, pending, files))
            for position, _ in batch:
                group = positions[position][1]
                self._group(prepared, generation, owner, group, stages[group['group_id']], identities, inputs)

    def _group(self, prepared, generation, owner, group, state, identities, inputs):
        if state['external_version'] != group['version']:
            raise ValueError('Musical group staged selection changed')
        if state['complete']:
            return
        version, count = state['id'], state['member_count']
        if count > len(group['members']):
            raise RuntimeError('Musical group membership prefix exceeds its retained plan')
        for batch in _batches(islice(group['members'], count, None)):
            self.repository.install_members(generation, owner, version, batch, identities, inputs)
        facts = self.repository.version_facts(version)
        if not content.equal(self._version_rows(facts), content.prepared_rows(prepared, group, identities.get)):
            raise ValueError('Musical group version digest collision or incomplete staged content')
        self.repository.seal(generation, owner, version)

    def _version_rows(self, facts):
        row = facts['version']
        yield canonical([content.FORMAT, row['external_key'], row['parent_key'],
                         self.files.read(row, 'definition'), self.files.policy(facts['policy'])])
        for kind, members in (('recording', facts['members']), ('unresolved', facts['unresolved'])):
            for identity, similarity, support, digest in members:
                yield canonical([[kind, identity], content.score(similarity), content.score(support), digest])

    def publish(self, prepared):
        owner = self.stage(prepared)
        self.repository.finish_staged(prepared['generation'], owner)
