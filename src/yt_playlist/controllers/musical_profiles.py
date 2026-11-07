"""Musical-profile file access, conflict policy and ordered publication."""
import hashlib
import json


class MusicalProfiles:
    def __init__(self, repository, files, metadata, predictions, refresh):
        self.repository, self.files, self.metadata = repository, files, metadata
        self.predictions, self.refresh = predictions, refresh

    def stale(self, version, limit=32):
        return self.repository.stale(version, limit)

    def queue_stale(self, version, limit=32):
        return self.repository.queue_stale(version, limit)

    def pending(self):
        return self.repository.pending()

    def rotate(self, key):
        return self.repository.rotate(key)

    def source_dependencies(self, key):
        return self.repository.source_dependencies(key)

    def source_input(self, key, limit=32, max_bytes=256 * 1024, *, observation_ids=None):
        with self.metadata.retained():
            facts = self.repository.source_facts(key, limit, max_bytes, observation_ids=observation_ids)
            if facts is None:
                return None
            source, pending, ids = facts
            source = dict(source)
            reference = source.pop('reference')
            source['observations'] = self.metadata.source_values(reference, max_bytes=max_bytes)
            return source, pending, ids

    def publish_source(self, key, pending, ids, payload, version, digest, now, limit=32):
        self.repository.assert_idle()
        with self.files.retained():
            owners = self.repository.publication_owners('source', [key])
            references = ([] if owners is None else self.files.write('source',
                [(key, owners[key], payload, version, digest)]))
            return self.repository.publish_source(key, pending, ids, payload, version, digest, now, limit,
                                                  references=references)

    def recording_inputs(self, keys, max_bytes=8 * 1024 * 1024, *, source_scopes=None):
        keys = list(keys)
        if len(keys) > 512:
            raise ValueError('Musical profile page exceeds 512 identities')
        with self.files.retained():
            facts = self.repository.recording_facts(keys, source_scopes=source_scopes,
                                                    max_sources=64, max_link_scan=4096)
            profiles = self.files.read_page('source', [ref for refs, _choices, _cut in facts.values() for ref in refs],
                                           max_bytes=max_bytes)
            result = {}
            for key, (references, choices, truncated) in facts.items():
                # Conflicting explicit choices block inherited fallback.
                overrides = {field: json.loads(next(iter(values))) if len(values) == 1 else None
                             for field, values in choices.items()}
                result[key] = ([profiles[ref[0]] for ref in references], overrides, truncated)
            return result

    def publish_recordings(self, rows, now):
        self.repository.assert_idle()
        prepared = []
        for key, payload, version, digest in rows:
            if payload is not None:
                profile = json.loads(payload)
                if 'vibes' in profile:
                    profile.pop('vibes')
                    payload = json.dumps(profile, sort_keys=True, separators=(',', ':'), allow_nan=False)
                    digest = hashlib.sha256(payload.encode()).hexdigest()
            prepared.append((key, payload, version, digest))
        with self.files.retained():
            owners = self.repository.publication_owners('recording',
                [key for key, payload, _version, _digest in prepared if payload is not None])
            references = ([] if owners is None else self.files.write('recording',
                [(key, owners[key], payload, version, digest) for key, payload, version, digest in prepared
                 if payload is not None]))
            changed = self.repository.publish_recordings(prepared, now, references=references)
        if changed:
            self.refresh.publish_notification()

    def readiness_fingerprint(self, keys):
        return self.readiness_state(keys)[0]

    def readiness_state(self, keys):
        digest, profiles = hashlib.sha256(), {}
        keys = sorted(set(keys))
        for offset in range(0, len(keys), 512):
            rows = self.repository.readiness_rows(keys[offset:offset + 512])
            digest.update(json.dumps(rows, separators=(',', ':')).encode())
            for key, policy, _digest, vibe, score, support, source, stamp in rows:
                profile = profiles.setdefault(key, {'version': policy, 'vibes': {
                    'inputs_version': stamp, 'fits': {}}})
                if vibe is not None and policy is not None:
                    profile['vibes']['fits'][vibe] = {
                        'score': score, 'support': support, 'source': source, 'inputs_version': stamp}
        return digest.hexdigest(), profiles

    def by_keys(self, keys):
        keys = list(keys)
        if len(keys) > 512:
            raise ValueError('Musical profile page exceeds 512 identities')
        with self.files.retained():
            result = self.files.read_page('recording', self.repository.profile_references(keys))
        return self.predictions.overlay(result)

    def provenance_sources(self, key, digests, *, limit=16, max_bytes=256 * 1024):
        if not digests:
            return [], False
        digests = list(dict.fromkeys(digests))[:64]
        with self.files.retained():
            rows = self.repository.provenance_references(key, digests, limit=limit)
            admitted, used = [], 0
            for row in rows:
                size = len(row[1].encode()) if row[1] else row[6]
                if len(admitted) == limit or used + size > max_bytes:
                    break
                admitted.append(row)
                used += size
            profiles = self.files.read_page('source', admitted, max_bytes=max_bytes)
            return [profiles[row[0]] for row in admitted], set(digests) != {row[3] for row in admitted}
