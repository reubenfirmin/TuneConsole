"""Coordinate paid-description admission and completion."""
import json
import uuid

from yt_playlist.core.album_description_files import MAX_BYTES
from yt_playlist.rec import paid_description_policy as policy


class AlbumDescriptions:
    def __init__(self, repository, policies):
        self.repository = repository
        self.policies = policies

    policy_version = staticmethod(policy.policy_version)

    def summary(self):
        return self.repository.summary('ready', policy.PENDING_STATES)

    def album_id(self, album):
        explicit, browse = album.get('album_id'), album.get('browse_id')
        resolved = self.repository.album_by_browse_id(browse) if browse else None
        if explicit is None:
            return resolved
        if type(explicit) is not int:
            return None
        explicit = self.repository.album_by_id(explicit)
        return None if browse and resolved != explicit else explicit

    def result(self, request_id):
        row = self.repository.request(request_id)
        if not row or row['outcome_json'] is None:
            return None
        return json.loads(row['outcome_json'])

    def cached(self, album_id, policy_version):
        request_id = self.repository.request_for(album_id, policy_version, policy.REQUEST_ORDER)
        return self.result(request_id) if request_id is not None else None

    def successful_result(self, album_id):
        raw = self.repository.successful_result(album_id)
        return json.loads(raw) if raw else None

    def legacy_result(self, identity):
        request_id = self.repository.legacy_request(identity)
        return self.result(request_id) if request_id is not None else None

    def requests_today(self, now):
        return self.repository.usage(policy.period(now))

    def availability(self, credential, now):
        policy.validate_time(now)
        return policy.availability(self.repository.access_rows(credential, now, 'auth'))

    def claim(self, album_id, configuration, credential, daily_limit, now):
        return self.claim_version(album_id, policy.policy_version(configuration), credential, daily_limit, now,
                                  configuration=configuration)

    def claim_version(self, album_id, version, credential, daily_limit, now, *, configuration=None):
        """Admit an exact policy, including already installed policy versions."""
        policy.validate_admission(album_id, version, credential, daily_limit, now)
        if configuration is not None:
            self.policies.publish([(version, json.dumps(configuration), now)])
        with self.repository.transaction():
            outcome = self._claim(album_id, version, credential, daily_limit, now)
        if outcome['status'] == 'cached':
            return {'status': 'cached', 'record': self.result(outcome['request_id'])}
        return outcome

    def _claim(self, album_id, version, credential, daily_limit, now):
        album_id = self.repository.canonical_album(album_id)
        request_id = self.repository.request_for(album_id, version, policy.REQUEST_ORDER)
        row = self.repository.request(request_id) if request_id is not None else None
        if row and row['status'] == 'ready':
            return {'status': 'cached', 'request_id': row['id']}
        blocked = policy.unattributed_request_block(self.repository.access_state('unattributed_request', ''), now)
        if blocked:
            return blocked
        active = self.repository.active_request(policy.ACTIVE_STATES)
        if active:
            if active['lease_until'] > now:
                return {'status': 'busy', 'retry_at': active['lease_until']}
            state = policy.expired_state(active)
            self.repository.expire_request(active['id'], state, state == 'uncertain', now)
            if row and row['id'] == active['id']:
                row = self.repository.request(row['id'])
        blocked = policy.admission(row, self.requests_today(now), self.availability(credential, now), daily_limit, now)
        if blocked:
            return blocked
        attempt, token = (row['attempt_count'] + 1 if row else 1), uuid.uuid4().hex
        request_id = self.repository.reserve(album_id, version, policy.request_key(album_id, version),
            row['id'] if row else None, attempt, token, credential, now + policy.LEASE_SECONDS,
            policy.period(now), now, initial_status='pending', claimed_status='claimed')
        return {'status': 'reserved', 'request_id': request_id, 'token': token, 'attempt': attempt}

    def authorize(self, request_id, token, credential, daily_limit, now):
        policy.validate_time(now)
        policy.validate_limit(daily_limit)
        with self.repository.transaction():
            row = self.repository.request(request_id)
            counted = self.repository.counted_attempt(request_id, row['attempt_count']) if row else None
            if not policy.can_authorize(row, token, credential, now, self.availability(credential, now),
                                        self.requests_today(now), daily_limit, counted):
                return False
            return self.repository.mark_sent(request_id, token, now + policy.LEASE_SECONDS, now,
                                             expected_status='claimed', status='sent')

    def finish(self, request_id, token, record, now):
        policy.validate_time(now)
        raw, tokens = policy.record(record)
        if len(raw.encode()) > MAX_BYTES:
            raise ValueError('Paid description result exceeds its byte limit')
        with self.repository.transaction():
            row = self.repository.request(request_id)
            if not policy.can_finish(row, token, record, tokens):
                return False
            decision = policy.completion(row, token, record)
            if decision['access'] is not None:
                access = decision['access']
                decision['access'] = policy.merge_access(access,
                    self.repository.access_state(access['scope'], access['credential']), now)
            self.repository.complete(request_id, row['attempt_count'], token, decision,
                raw, tokens, policy.period(now), now)
        return True
