"""Group read models assembled from SQL facts and immutable file documents."""
from yt_playlist.core import group_evidence
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.group_storage import policy_version
from yt_playlist.controllers.taste_groups import TasteGroups
from yt_playlist.controllers.group_publication import GroupPublication


class CompactTasteGroups(TasteGroups):
    def __init__(self, repository, files, profiles, retention, policies=None):
        super().__init__(repository, profiles)
        self.files = files
        self.retention = retention
        self.publication = GroupPublication(repository, files, policies)

    def head_id(self):
        return self.repository.head_id()

    def has_adaptive_head(self):
        return self.repository.has_adaptive_head()

    def head(self, *, members=False):
        with self.files.retained():
            return self._head(self.repository.head(members=members))

    def preview_head(self):
        with self.files.retained():
            head = self._head(self.repository.preview_head())
            if head is not None:
                head['payload'].pop('recording_inputs', None)
            return head

    def _head(self, row):
        if row is None:
            return None
        payload = self.files.read(row, 'generation')
        return {'generation': row['external_key'], 'policy': canonical(self.files.policy(row['policy'])).decode(),
                'fingerprint': row['input_fingerprint'], 'created_at': row['created_at'],
                'serial': row['input_serial'], 'payload': payload,
                'groups': [self._definition(group) for group in row['groups']]}

    def head_checkpoint(self):
        with self.files.retained():
            row = self.repository.head_checkpoint()
            return {**row, 'policy': canonical(self.files.policy(row['policy'])).decode()} if row else None

    def _definition(self, row):
        result = {**self.files.read(row, 'definition'), 'group_id': row['external_key'],
                  'version': row['external_version'], 'parent_version': row['parent_version']}
        if 'members' in row:
            result['members'] = row['members']
        if 'generation' in row:
            result.update(generation=row['generation'], created_at=row['created_at'])
        return result

    def definitions(self, generation, *, members=False):
        with self.files.retained():
            return [self._definition(row) for row in self.repository.definitions(generation, members=members)]

    def recording_groups(self, generation):
        return self.repository.recording_groups(generation)

    def definition(self, version, *, generation=None):
        with self.files.retained():
            row = self.repository.definition(version, generation=generation)
            return self._definition(row) if row else None

    def learning_head(self):
        with self.files.retained():
            row = self.repository.learning_head()
            if row is None:
                return None
            return {**row, 'groups': [
                {key: group[key] for key in ('group_id', 'version', 'parent_version', 'legacy', 'leaf')}
                | {'genres': self.files.read(group, 'definition').get('genres', [])}
                for group in row['groups']]}

    def correction_definition(self, version, *, generation=None):
        return self.repository.correction_definition(version, generation=generation)

    def cleanup_builds(self):
        return self.repository.cleanup_builds()

    def cleanup_staged(self):
        return self.repository.cleanup_builds() or self.retention.step()

    def stage(self, prepared):
        return self.publication.stage(prepared)

    def resumable_build(self, fingerprint, serial, policy, parent):
        return self.repository.resumable_build(fingerprint, serial, policy_version(policy), parent)

    def publish(self, prepared):
        return self.publication.publish(prepared)

    def retire_history(self):
        return self.retention.step()

    def retain_evidence(self, kind, identity, context, created_at, *, strict=True):
        owner = group_evidence.prepare(kind, identity, context, created_at)
        return self.repository.retain_evidence(owner, strict=strict)

    def release_evidence(self, kind, identity):
        return self.repository.release_evidence(kind, identity)
