"""Validate immutable policy messages, publish files, then commit SQL receipts."""
import json
from contextlib import ExitStack

from yt_playlist.core import policy_config, policy_documents
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.policy_history_read import unpack


class Policies:
    def __init__(self, repository, files, guard):
        self.repository, self.files, self.guard = repository, files, guard

    def publish(self, rows, bases=(), *, enforce_budget=False):
        rows, bases = list(rows), list(bases)
        if not rows and not bases:
            return
        if enforce_budget and (sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases) > policy_config.MAX_BATCH_BYTES
                               or max(len(rows), len(bases)) > policy_config.MAX_BATCH_VARIANTS):
            raise policy_config.PolicyStorageFull('Prepared policy batch exceeds its storage budget; retaining published pools')
        capacity = (policy_config.MAX_UNUSED_VARIANTS, policy_config.MAX_UNUSED_BYTES) if enforce_budget else None
        with self.guard(), ExitStack() as retained:
            if not self.repository.file_owned():
                links = [(version, value[BASE_FIELD]) for version, raw, _created in rows
                         if BASE_FIELD in (value := json.loads(raw))]
                return self.repository.publish_legacy(rows, bases, links, capacity=capacity)
            retained.enter_context(self.files.retained())
            prepared = {True: {}, False: {}}
            for body, records in ((True, bases), (False, rows)):
                for record in records:
                    version, raw, *tail = record
                    created = tail[-1]
                    value, digest = policy_documents._record(version, raw, created)
                    if body and (type(tail[0]) is not int or tail[0] != len(raw.encode())):
                        raise ValueError('Invalid shared policy byte size')
                    base = None if body else value.get(BASE_FIELD)
                    if base is not None and (not isinstance(base, str) or not base):
                        raise ValueError('Invalid shared recommendation policy reference')
                    previous = prepared[body].get(version)
                    if previous is not None and previous['digest'] != digest:
                        raise ValueError('Conflicting shared recommendation policy version' if body
                                         else 'Conflicting recommendation policy version')
                    prepared[body][version] = dict(body=body, version=version, value=value, digest=digest,
                                                   created=created, base=base)
            facts = self.repository.records(prepared[False], set(prepared[True]) |
                {record['base'] for record in prepared[False].values() if record['base'] is not None})
            pending = []
            for body, name in ((True, 'bodies'), (False, 'policies')):
                for record in prepared[body].values():
                    existing = facts[name].get(record['version'])
                    if existing is not None and existing['content_digest'] != record['digest']:
                        raise ValueError(f"Conflicting immutable recommendation policy: {record['version']!r}")
                    if record['base'] is not None and record['base'] not in prepared[True] and record['base'] not in facts['bodies']:
                        raise ValueError(f"Missing shared recommendation policy body: {record['base']!r}")
                    if existing is None:
                        if not body:
                            archived = self.files.archived(record['version'], self.repository.archived(record['version']))
                            if archived is not None and archived['configuration'] != record['value']:
                                raise ValueError(f"Conflicting immutable historical policy: {record['version']!r}")
                        pending.append(record)
            if capacity is not None:
                count, size = self.repository.unused_inventory()
                added_count = sum(not record['body'] for record in pending)
                added_bytes = sum(len(canonical({'format': policy_documents.FORMAT,
                                                'configuration': record['value']})) for record in pending)
                if count + added_count > capacity[0] or size + added_bytes > capacity[1]:
                    raise policy_config.PolicyCapacityFull(count, size, added_count, added_bytes)
            references = self.files.publish_many([record['value'] for record in pending])
            if len(references) != len(pending):
                raise ValueError('Policy publication changed its record count')
            installed = []
            for record, (filename, digest, size, offset) in zip(pending, references, strict=True):
                if digest != record['digest']:
                    raise ValueError('Policy publication changed a configuration')
                installed.append({key: value for key, value in record.items() if key != 'value'} |
                                 dict(filename=filename, size=size, offset=offset))
            # Include existing versions in missing-impression repair as well.
            for record in prepared[False].values():
                if record['version'] in facts['policies']:
                    row = facts['policies'][record['version']]
                    installed.append({key: value for key, value in record.items() if key != 'value'} |
                        dict(filename=row['configuration_filename'], size=row['byte_size'], offset=row['configuration_offset']))
            self.repository.publish(installed)

    def captured(self, versions):
        versions = sorted(set(versions) - {None})
        if not self.repository.file_owned():
            return self.repository.legacy_capture(versions)
        with self.files.retained():
            facts = self.repository.records(versions)
            bases = {row['base_version'] for row in facts['policies'].values() if row['base_version'] is not None}
            bodies = self.repository.records((), bases)['bodies']
            rows, captured = [], []
            for version in versions:
                row = facts['policies'].get(version)
                if row is None:
                    continue
                value = self.files.read(row)
                if value.get(BASE_FIELD) != row['base_version']:
                    raise ValueError('Policy configuration does not match its shared-body reference')
                rows.append((version, canonical(value).decode(), row['created_at']))
            for version in sorted(bases):
                row = bodies.get(version)
                if row is None:
                    raise ValueError(f'Missing captured policy body: {version}')
                raw = canonical(self.files.read(row)).decode()
                captured.append((version, raw, len(raw.encode()), row['created_at']))
            return rows, captured

    def configuration(self, version):
        rows, bases = self.captured([version])
        if not rows:
            if not self.repository.file_owned():
                return None
            with self.files.retained():
                saved = self.files.archived(version, self.repository.archived(version))
                return unpack(saved) if saved else None
        value = json.loads(rows[0][1])
        base = value.pop(BASE_FIELD, None)
        return {**json.loads(bases[0][1]), **value} if base is not None else value

    def configuration_status(self, version):
        header = self.repository.archived(version)
        return header['state'] if header else 'missing'

    def retention_candidates(self, before, *, limit=None, cursor_key=None):
        protected = policy_config.protected_references(self.repository.legacy_root_messages())
        return self.repository.retention_candidates(before, protected=protected,
            limit=policy_config.SCAN_LIMIT if limit is None else limit, byte_limit=policy_config.SCAN_BYTES,
            cursor_key=policy_config.CURSOR if cursor_key is None else cursor_key)

    def retirement_pending(self, before):
        protected = policy_config.protected_references(self.repository.legacy_root_messages())
        return self.repository.retirement_pending(before, protected=protected)

    def restart_retirement(self):
        self.repository.clear_retirement_cursor(policy_config.CURSOR)

    def prune(self, versions, cursor, *, cursor_key=None):
        with self.guard():
            protected = policy_config.protected_references(self.repository.legacy_root_messages())
            return self.repository.prune([version for version in versions if version not in protected], cursor,
                cursor_key=policy_config.CURSOR if cursor_key is None else cursor_key,
                limit=policy_config.SCAN_LIMIT, byte_limit=policy_config.SCAN_BYTES)

    def prepare_history(self, now, *, limit=None):
        limit = policy_config.HISTORY_BATCH if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= policy_config.HISTORY_BATCH:
            raise ValueError('Policy history batch must be 1 to 128')
        before = now - policy_config.GRACE_SECONDS
        versions = self.repository.history_candidates(before, limit)
        if not versions:
            return 0
        with self.guard(), self.files.retained():
            rows, bodies = self.repository.history_records(before, versions)
            publication, archived = self.files.archive_many(self.repository.history_publication(), rows, bodies)
            if archived:
                self.repository.publish_history(publication, archived)
            return len(archived)

    def maintain_history(self, now):
        if not self.repository.history_available():
            return 0
        converted = self.prepare_history(now)
        with self.guard():
            versions = self.repository.expiring_history(now - policy_config.HISTORY_RETAIN_DAYS * 86400,
                                                       policy_config.HISTORY_BATCH)
            if versions:
                with self.files.retained():
                    publication, versions = self.files.expire_history(self.repository.history_publication(), versions)
                    self.repository.expire_history(publication, versions)
            self.files.collect_history(self.repository.history_publication(),
                now - policy_config.HISTORY_FILE_GRACE_DAYS * 86400, limit=policy_config.HISTORY_FILE_SCAN_LIMIT)
        return converted + len(versions)
