"""Library progress policy and ordered attempt publication."""
from yt_playlist.core.sync_state import restore_progress
from yt_playlist.library.sync_outcomes import stamp_result


class LibrarySync:
    def __init__(self, repository, recovery):
        self.repository, self.recovery = repository, recovery

    def state(self):
        with self.recovery.retained():
            facts = self.repository.state_facts()
            attempt = facts['attempt']
            if attempt is None or attempt['recovery_filename'] is None:
                return {}
            result = self.recovery.read(
                (attempt['recovery_filename'], attempt['recovery_digest'], attempt['recovery_bytes']),
                kind='library-sync', owner=1)
            return restore_progress(result, attempt, facts['cursors'])

    def begin_attempt(self, now):
        return self.repository.begin_attempt(now)

    def checkpoint(self, now, result, *, expected_revision, previous):
        return self.finish_attempt(now, result, expected_revision=expected_revision,
                                   previous=previous, checkpoint=True)

    def finish_attempt(self, now, result, *, expected_revision=None, previous=None, checkpoint=False):
        self.repository.require_revision(expected_revision)
        from copy import deepcopy
        result = stamp_result(deepcopy(result), self.state() if previous is None else previous, now)
        self.repository.ensure_attempt(now)
        with self.recovery.retained():
            integrity = self.recovery.describe('library-sync', 1, result)
            file_id = self.repository.reserve_file(integrity)
            reference = self.recovery.publish_reserved(file_id, 'library-sync', 1, result, expected=integrity)
            self.repository.finish_attempt(
                now, result, reference=reference, file_id=file_id, expected_revision=expected_revision,
                last_success=now if result['status'] == 'complete' and not checkpoint else None,
                checkpoint=checkpoint)
        return result
