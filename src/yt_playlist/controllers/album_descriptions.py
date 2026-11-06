"""Coordinate paid-description decisions, request SQL and immutable files."""
import json
import uuid

from yt_playlist.rec import paid_description_policy as policy


class AlbumDescriptions:
    def __init__(self, repository, read_result, publish_result, publish_policy, *, proposals=None, installed=True):
        self.repository = repository
        self.read_result = read_result
        self.publish_result = publish_result
        self.publish_policy = publish_policy
        self.proposals = proposals
        self.installed = installed

    policy_version = staticmethod(policy.policy_version)

    def summary(self):
        if self.installed:
            return self.repository.summary('ready', policy.PENDING_STATES)
        notes = self.proposals.get_proposals('album_notes') or {}
        completed = sum(bool((note.get('description') or {}).get('kind') == 'generated') for note in notes.values())
        return completed, bool(self.proposals.get_proposals('album_note_requests'))

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
        if not row or row['outcome_filename'] is None:
            return None
        return json.loads(self.read_result(row['outcome_filename'], row['outcome_digest']))

    def cached(self, album_id, policy_version):
        request_id = self.repository.request_for(album_id, policy_version, policy.REQUEST_ORDER)
        return self.result(request_id) if request_id is not None else None

    def successful_result(self, album_id):
        reference = self.repository.successful_reference(album_id)
        return json.loads(self.read_result(*reference)) if reference else None

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
        reference = self.publish_policy(configuration) if configuration is not None else None
        with self.repository.transaction():
            outcome = self._claim(album_id, version, credential, daily_limit, now, reference)
        if outcome['status'] == 'cached':
            return {'status': 'cached', 'record': self.result(outcome['request_id'])}
        return outcome

    def _claim(self, album_id, version, credential, daily_limit, now, reference):
        album_id = self.repository.canonical_album(album_id)
        request_id = self.repository.request_for(album_id, version, policy.REQUEST_ORDER)
        row = self.repository.request(request_id) if request_id is not None else None
        if row and row['status'] == 'ready':
            return {'status': 'cached', 'request_id': row['id']}
        blocked = policy.legacy_block(self.repository.legacy_usage(), now)
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
            policy.period(now), now, initial_status='pending', claimed_status='claimed', policy_reference=reference)
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
        row = self.repository.request(request_id)
        if not policy.can_finish(row, token, record, tokens):
            return False
        reference = self.publish_result(raw)
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
                reference, tokens, policy.period(now), now, model=record['model'] if record['status'] == 'ready' else None)
        return True
