"""Prepare group files before committing their bounded SQL ownership."""
from itertools import islice

from yt_playlist.core import group_content as content, group_plan, group_storage
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.group_publication import _batches, _BuildBeingCleaned


class GroupPublication:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def stage(self, prepared):
        self.repository.assert_idle()
        identities = self.repository.recording_ids(group_plan.recording_keys(prepared))
        inputs, plans, policy, metadata = group_plan.prepare(prepared, identities)
        self.repository.clear_build_conflicts(prepared, plans)
        with self.files.retained():
            policy_reference = self.files.write_policy(policy)
            metadata_reference, = self.files.write([('generation', metadata)])
            while True:
                try:
                    generation, manifest, owner, ready = self.repository.claim_generation(prepared, inputs,
                        group_storage.policy_version(prepared['policy']), policy_reference, metadata_reference)
                    break
                except _BuildBeingCleaned:
                    while self.repository.cleanup_builds(generation=prepared['generation']):
                        check_cancellable_work()
            if ready:
                return owner
            self.repository.install_inputs(generation, manifest, owner, inputs)
            self.repository.install_identities(prepared, generation, owner)
            self._groups(prepared, generation, owner, plans, identities)
            self._lineage(prepared, generation, owner)
            self.repository.install_caches(prepared, generation, owner, identities)
            self.repository.mark_ready(generation, owner, len(plans))
            return owner

    def _groups(self, prepared, generation, owner, plans, identities):
        positions = {row[0]: row for row in plans}
        for batch in _batches((row[0], row[1]['group_id'], row[4], row[1]['payload']) for row in plans):
            pending = self.repository.definition_candidates(generation, owner, batch)
            files = self.files.write(('definition', positions[position][2]) for position, _group_id in pending)
            self.repository.install_definitions(prepared, generation, owner,
                [(positions[position], group_id, file) for (position, group_id), file in zip(pending, files, strict=True)])
            for position, _key, _digest, _raw in batch:
                self._group(prepared, generation, owner, positions[position], identities)

    def _group(self, prepared, generation, owner, plan, identities):
        position, group, value, _fields, _digest = plan
        state = self.repository.group_state(generation, owner, plan)
        if state is None:
            return
        version = state['version_id']
        if not state['sealed']:
            count, representatives = state['members'], state['representatives']
            if count > len(group['members']) or representatives > len(value.get('rep_keys', [])):
                raise RuntimeError('Musical group content prefix exceeds its retained plan')
            inputs = content._prepared_document(prepared, 'payload', 'Musical group generation').get('recording_inputs', {})
            for batch in _batches(islice(group['members'], count, None)):
                self.repository.install_members(generation, owner, version, batch, identities, inputs)
            for batch in _batches(enumerate(value.get('rep_keys', [])[representatives:], representatives)):
                self.repository.install_representatives(generation, owner, version, batch, identities)
        facts = self.repository.version_facts(version)
        if not content.equal(self._version_rows(facts), content.prepared_rows(prepared, group, identities.get)):
            raise ValueError('Musical group version digest collision or incomplete staged content')
        self.repository.link_group(generation, owner, state['group_id'], version, position, group['version'])

    def _version_rows(self, facts):
        row = facts['version']
        yield canonical([content.FORMAT, row['external_key'], row['parent_key'],
                         self.files.read(row, 'definition'), self.files.policy(facts['policy'])])
        for kind, members in (('recording', facts['members']), ('unresolved', facts['unresolved'])):
            for identity, similarity, support, digest in members:
                yield canonical([[kind, identity], content.score(similarity), content.score(support), digest])

    def _lineage(self, prepared, generation, owner):
        count = self.repository.lineage_prefix(generation)
        if count > len(prepared['lineage']):
            raise RuntimeError('Musical group lineage prefix exceeds its retained plan')
        for batch in _batches(islice(prepared['lineage'], count, None)):
            existing = self.repository.lineage_facts([(source, target) for source, target, _relation, _raw in batch])
            values = [(source, target, relation, group_plan.lineage(raw)) for source, target, relation, raw in batch]
            pending = [row for row in values if (row[0], row[1]) not in existing]
            files = iter(self.files.write(('lineage', row[3]) for row in pending))
            rows = []
            for source, target, relation, value in values:
                previous = existing.get((source, target))
                if previous is None:
                    reference = next(files)
                else:
                    if previous['relation'] != relation or self.files.read(previous, 'lineage') != value:
                        raise ValueError('Temporal musical group evidence changed')
                    reference = tuple(previous['evidence_' + field] for field in ('filename', 'digest', 'bytes', 'offset'))
                rows.append((source, target, relation, value, reference))
            self.repository.install_lineage(generation, owner, rows)

    def publish(self, prepared):
        owner = self.stage(prepared)
        self.repository.finish_staged(prepared['generation'], owner)
