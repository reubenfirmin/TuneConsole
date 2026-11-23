"""Account receipts: validate intent, write evidence, then persist SQL state."""
import json

from yt_playlist.core import workflow_settings as workflows


class WorkflowSettings:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def _read(self, row, kind):
        reference = tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
        return self.files.read(reference, kind=kind, owner=row['id'])

    def receipts(self, account, *, kind='playlist', token=False):
        with self.files.retained():
            rows = self.repository.workflow_documents(kind, account)
            return {row['fingerprint']: workflows.restore_receipt(row, self._read(row, 'spotify-transfer'), row['slots'], token=token)
                    for row in rows}

    def get(self, key, default=None):
        kind, account = workflows.receipt_key(key)
        value = self.receipts(account, kind=kind)
        return json.dumps(sorted(value) if kind == 'album' or account is None else value) if value else default

    def prepare_values(self, values):
        plans, next_ids = [], {}
        for key, raw in values.items():
            value = json.loads(raw) if raw is not None and raw != '' else None
            kind, account = workflows.receipt_key(key)
            if value is None:
                plans.append(('delete', (kind, account), None))
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                plans.extend(self._receipt(kind, account, fingerprint, None, next_ids, legacy=account is None)
                             for fingerprint in dict.fromkeys(value))
            elif isinstance(value, dict) and kind == 'playlist':
                plans.extend(self._receipt(kind, account, fingerprint, item, next_ids) for fingerprint, item in value.items())
            else:
                raise ValueError('Unsupported Spotify workflow receipt family')
        return self._publish(plans)

    def _receipt(self, kind, account, fingerprint, value, next_ids, *, legacy=False, expected=None):
        facts = self.repository.workflow_write_facts(kind, account, fingerprint)
        facts = {**facts, 'next_id': max(facts['next_id'], next_ids.get('receipt', 1))}
        if facts['prior'] is None:
            next_ids['receipt'] = facts['next_id']+1
        prepared = workflows.prepare_receipt(kind, account, fingerprint, value, facts, legacy=legacy, expected_revision=expected)
        return 'receipt', prepared, None

    def _publish(self, plans):
        result = []
        for record, prepared, _ in plans:
            reference = None
            if record == 'receipt':
                kind = 'spotify-transfer'
                integrity = self.files.describe(kind, prepared['identity'], prepared['value'])
                prepared['file_id'] = self.repository.reserve_file(kind, prepared['identity'], integrity)
                reference = self.files.publish_reserved(prepared['file_id'], kind, prepared['identity'], prepared['value'],
                                                        expected=integrity)
            result.append((record, prepared, reference))
        return result

    def save_transfer(self, account, fingerprint, value):
        with self.files.retained():
            prepared = self._receipt('playlist', account, fingerprint, value, {}, expected=value.get(workflows.TOKEN, ''))
            plans = self._publish([prepared])
            return self.repository.save_workflows(plans)
