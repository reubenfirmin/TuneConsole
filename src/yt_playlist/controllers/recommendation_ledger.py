"""Recommendation event meaning, editorial rotation and reports."""
import json

from yt_playlist.core import policy_config, mix_choice_policy, served_facts
from yt_playlist.core.evidence_ledger import library_byte_budget
from yt_playlist.rec.scorecard_attribution import Attribution

RESPONSE_RETAIN_DAYS = 90
RETENTION_BATCH_SIZE = 256
POLICY_RETENTION_CURSOR = 'rec_policy_retention_cursor'


class RecommendationLedger:
    def __init__(self, repository, policies, guard, *, ledger_factory, playback, impressions):
        self.repository, self.policies, self.guard = repository, policies, guard
        self.ledger_factory = ledger_factory
        self.playback = playback
        self.impressions = impressions

    def scorecard_inputs(self, start, end, outcome_window_h=24,
                         impression_id=None, experiments_only=False):
        repository = self.repository
        impressions = repository.scorecard_impressions(
            start, end, impression_id=impression_id, experiments_only=experiments_only)
        for row in impressions:
            fact = row.pop('facts', None)
            if fact is not None:
                row['provenance'] = json.dumps(served_facts.reporting_trace(fact))
        keys = {row['identity_key'] for row in impressions}
        horizon = max(0., float(outcome_window_h)) * 3600.
        if not keys:
            return impressions, Attribution([], [], [], [], [], horizon)
        facts = repository.scorecard_facts()
        owners = facts.owners(keys, start - horizon, end + horizon, policy_kind='live')
        plays, responses = facts.outcomes(keys, start, end + horizon)
        exits = facts.exits(start, end + horizon, kinds=('track_exit', 'bye', 'ended'))
        videos = facts.video_owners({row['video_id'] for row in exits if row['video_id']},
                                   start - horizon, end + horizon, policy_kind='live')
        attribution = Attribution(owners, videos, plays, responses, exits, horizon)
        missing = attribution.missing_videos()
        # Earlier live snapshots retain ownership after later catalog edits.
        # Legacy reports without snapshots use owned entries before discovery.
        for discovered in (False, True):
            unresolved = missing - attribution.fallback.keys()
            if unresolved:
                attribution.add_fallback(facts.catalog_keys(unresolved, discovered=discovered))
        return impressions, attribution

    def served_recommendation_inputs(self, impression_id):
        result = self.impressions.facts(impression_id)
        if result is None:
            return None
        result['configuration'] = self.policies.configuration(result['model_version'])
        result['configuration_status'] = ('available' if result['configuration'] is not None else
                                          self.policies.configuration_status(result['model_version']))
        pool = result['provenance'].get('pool_policy')
        result['pool_configuration'] = self.policies.configuration(str(pool)) if pool else None
        inputs = (None if result['provenance'].get('policy_kind') == 'counterfactual' else
                  self.scorecard_inputs(result['served_at'], result['served_at'] + 1,
                                        impression_id=impression_id))
        return result, inputs

    def experiment_inputs(self, start, end, mature_end):
        inputs = self.scorecard_inputs(start, mature_end, experiments_only=True)
        return inputs, self.repository.experiment_shadows(start, end)

    def maintain_policy_history(self, now):
        return self.policies.maintain_history(now)

    def prepare_policy_history(self, now, *, limit=None):
        return self.policies.prepare_history(now, limit=limit)

    def publish_mix_menu(self, record, context):
        if not self.repository.mix_choices_available():
            return record
        plan = mix_choice_policy.menu_plan(record, context)
        self.repository.publish_mix_menu(plan)
        return self._mix_projection(record)

    def expand_mix_options(self, options):
        return [({**self.repository.mix_descriptor(option['descriptor_id']), **option}
                 if 'descriptor_id' in option else mix_choice_policy.visible_option(option))
                for option in options]

    def policy_retention_candidates(self, now):
        return self.policies.retention_candidates(now - policy_config.GRACE_SECONDS)

    def policy_retirement_pending(self, now):
        return self.policies.retirement_pending(now - policy_config.GRACE_SECONDS)

    def restart_policy_retirement(self):
        return self.policies.restart_retirement()

    def prune_policies(self, versions, cursor):
        return self.policies.prune(versions, cursor)

    def mix_descriptor(self, descriptor_id):
        return self.repository.mix_descriptor(descriptor_id)

    def album_playback_context(self):
        return self.playback.album()

    def remember_album_playback_context(self, value):
        return self.playback.remember_album(value)

    def bind_album_playback_context(self, pending, account_id):
        return self.playback.bind_album(pending, account_id)

    def has_served_request(self, request_id):
        return self.repository.has_served_request(request_id)

    def bind_choice_playback(self, account_id, playlist, video, opportunity, option, now, window_s):
        return self.playback.bind_choice(account_id, playlist, video, opportunity, option, now, window_s)

    def choice_playback(self, account_id, playlist, video, now, window_s):
        return self.playback.choice(account_id, playlist, video, now, window_s)

    def evidence(self):
        return self.ledger_factory(library_byte_budget(self.repository.library_recording_count()),
                                   opportunity_projection=self._mix_projection)

    def _mix_projection(self, record):
        if not record or 'context' not in record:
            return record
        facts = self.repository.mix_projection_facts(record.get('id'), record['context'],
                                                     mixes=record.get('format') == 'mixes')
        return mix_choice_policy.project(record, facts)

    def publish_prepared_policies(self, rows, bases=()):
        return self.policies.publish(rows, bases, enforce_budget=True)

    def policy_configuration(self, version):
        return self.policies.configuration(version)

    def captured_policies(self, versions):
        return self.policies.captured(versions)

    def record_serving_policy(self, version, configuration, now):
        return self.policies.publish([(version, json.dumps(configuration, sort_keys=True), now)])

    def record_prepared_policies(self, rows, bases=()):
        return self.policies.publish(rows, bases, enforce_budget=True)

    def record_recommendation_outcomes(self, event_id, kind, keys, now, generated=False):
        if kind not in {'open', 'start', 'save', 'promotion'}:
            raise ValueError('unsupported recommendation response')
        rows = [(event_id, kind, key, now, int(generated)) for key in set(keys) if key]
        self.repository.insert_recommendation_outcomes(rows)
        self._prune_response_batch(now - RESPONSE_RETAIN_DAYS * 86400)

    def record_served_impressions(self, request_id, surface, items, now, prune_before=None, *, policies=(), bases=()):
        rows, versions = [], []
        for item in items:
            key = item.get('identity_key') or item.get('key')
            if not key:
                continue
            model = item.get('model_version') or 'unknown'
            provenance = item.get('provenance') or {}
            rows.append((request_id, surface, item.get('lane') or '', item.get('mode_id'), key,
                         int(item['rank']), item.get('score'), model, json.dumps(provenance, sort_keys=True), now))
            pool = provenance.get('pool_policy')
            versions.extend((model, str(pool) if pool is not None else None))
        with self.guard():
            if policies or bases:
                self.policies.publish(policies, bases)
            self.impressions.record(rows, versions=versions)
        if prune_before is not None:
            self._prune_impression_batch(prune_before)
            self._prune_response_batch(prune_before)
            self._prune_policy_batch(prune_before)

    def _prune_impression_batch(self, before):
        return self.repository._prune_impression_batch(before)

    def _prune_response_batch(self, before):
        return self.repository._prune_response_batch(before)

    def _prune_policy_batch(self, before):
        with self.guard():
            versions, cursor = self.policies.retention_candidates(before, limit=RETENTION_BATCH_SIZE,
                                                                 cursor_key=POLICY_RETENTION_CURSOR)
            return self.policies.prune(versions, cursor, cursor_key=POLICY_RETENTION_CURSOR)

    def lane_impression_counts(self, since=None):
        return self.repository.lane_impression_counts(since)

    def maintain_impressions(self, now):
        return self.impressions.maintain(now, before=now - RESPONSE_RETAIN_DAYS * 86400)

    def story_views(self):
        return self.repository.story_views()

    def record_story_views(self, subjects, now):
        return self.repository.record_story_views(set(subjects), now, surface='into_recently', cooldown_s=6 * 3600)

    def bump_card_view(self, card, now):
        return self.repository.bump_card_view(card, now)

    def card_views(self, card):
        return self.repository.card_views(card)

    def refresh_card(self, card, cap, now):
        cap = max(1, cap)
        current = self.repository.card_views(card)
        views = (max(0, current - 1) // cap + 1) * cap + 1
        self.repository.set_card_views(card, views, now)
