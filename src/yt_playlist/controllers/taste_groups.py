"""Group preparation inputs and persistence operations over explicit owners."""


class TasteGroups:
    def __init__(self, repository, profiles):
        self.repository, self.profiles = repository, profiles
        self.staged_tables = repository.staged_tables

    def corpus_page(self, after='', *, limit=128, max_bytes=8 * 1024 * 1024):
        with self.profiles.retained():
            rows = self.repository.corpus_page(after, limit=limit, max_bytes=max_bytes)
            profiles = self.profiles.read_page('recording', [ref for _key, _inputs, ref in rows if ref is not None],
                                               max_bytes=max_bytes)
            return [(key, inputs, profiles.get(key)) for key, inputs, _ref in rows]

    def head(self, *, members=False):
        return self.repository.head(members=members)

    def preview_head(self):
        return self.head(members=True)

    def head_checkpoint(self):
        return self.repository.head_checkpoint()

    def definitions(self, generation, *, members=False):
        return self.repository.definitions(generation, members=members)

    def definition(self, version, *, generation=None):
        return self.repository.definition(version, generation=generation)

    def lineage(self):
        return self.repository.lineage()

    def successors(self, version, *, limit=512):
        return self.repository.successors(version, limit=limit)

    def head_generation(self):
        return self.repository.head_generation()

    def leaf_members(self, generation):
        return self.repository.leaf_members(generation)

    def legacy_versions(self):
        return self.repository.legacy_versions()

    def preparation_progress(self):
        return self.repository.preparation_progress()

    def serial(self):
        return self.repository.serial()

    def corpus_size(self, max_rows):
        return self.repository.corpus_size(max_rows)

    def stage(self, prepared):
        return self.repository.stage(prepared)

    def cleanup_staged(self):
        return self.repository.cleanup_staged()

    def abandon_staged(self, prepared):
        return self.repository.abandon_staged(prepared)

    def expired_builds(self):
        return self.repository.expired_builds()

    def resumable_build(self, fingerprint, serial, policy, parent):
        return self.repository.resumable_build(fingerprint, serial, policy, parent)

    def staged_at(self, generation):
        return self.repository.staged_at(generation)

    def finish_staged(self, generation, owner):
        return self.repository.finish_staged(generation, owner)

    def finish_preparation(self, generation, owner, serial, now):
        self.repository.finish_staged(generation, owner)
        self.checkpoint_preparation(generation, serial, now)

    def checkpoint_preparation(self, generation, serial, now):
        self.repository.checkpoint_preparation(generation, serial, now)

    def publish(self, prepared):
        return self.repository.publish(prepared)
