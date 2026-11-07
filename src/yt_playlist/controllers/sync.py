"""Library progress policy and ordered attempt publication."""
import json

from yt_playlist.library.sync_outcomes import stamp_result


def restore_progress(result, attempt, cursors):
    """Overlay durable cursor progress without reviving retired report members."""
    for row in cursors:
        account = (result.get('accounts', {}).get(str(row['account_id']))
                   if row['account_id'] is not None else result)
        if account is None:
            continue
        if row['collection'] == 'account':
            item = account
        elif row['collection'] == 'playlist':
            item = account.get('resources', {}).get('playlists', {}).get('items', {}).get(row['provider_playlist_id'])
            if item is None:
                continue
        else:
            item = account.setdefault('resources', {}).setdefault(row['collection'], {})
        for column, target in (('phase', 'status'), ('updated_at', 'last_attempt_at'),
                               ('last_success_at', 'last_success_at'), ('failures', 'failures'), ('retry_at', 'retry_at')):
            if row[column] is None:
                item.pop(target, None)
            else:
                item[target] = row[column]
        if row['cursor'] is not None:
            item['cursor'] = row['cursor']
    result['last_attempt_at'] = attempt['finished_at']
    if attempt['phase'] != 'running':
        result['status'] = attempt['phase']
    return result


class LibrarySync:
    def __init__(self, repository, settings, recovery, read_document):
        self.repository, self.settings = repository, settings
        self.recovery, self.read_document = recovery, read_document

    def state(self):
        with self.recovery.retained():
            facts = self.repository.state_facts()
            if 'document' in facts:
                raw = self.read_document(facts['document']) if facts['document'] is not None else None
                return json.loads(raw or '{}')
            if 'raw' in facts:
                return json.loads(facts['raw'])
            attempt = facts['attempt']
            if attempt is None or attempt['recovery_filename'] is None:
                return {}
            result = self.recovery.read(
                (attempt['recovery_filename'], attempt['recovery_digest'], attempt['recovery_bytes']),
                kind='library-sync', owner=1)
            return restore_progress(result, attempt, facts['cursors'])

    def begin_attempt(self, now):
        if self.repository.has_attempt_state():
            return self.repository.begin_attempt(now)
        self.settings.set_setting('last_sync_attempt_at', str(now))

    def finish_attempt(self, now, result, *, expected_revision=None):
        self.repository.require_revision(expected_revision)
        result = stamp_result(result, self.state(), now)
        if self.repository.has_attempt_state():
            self.repository.ensure_attempt(now)
            reference = self.recovery.publish('library-sync', 1, result)
            self.repository.finish_attempt(
                now, result, reference=reference, expected_revision=expected_revision,
                last_success=now if result['status'] == 'complete' else None)
        else:
            values = {'library_sync_state': json.dumps(result), 'last_sync_outcome': result['status']}
            if result['status'] == 'complete':
                values['last_sync_at'] = str(now)
            self.settings.publish_values(values)
        return result
