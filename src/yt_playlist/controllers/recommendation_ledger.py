"""Recommendation event meaning, editorial rotation and reports."""
import json

from yt_playlist.rec.scorecard_attribution import Attribution

RESPONSE_RETAIN_DAYS = 90


class RecommendationLedger:
    def __init__(self, repository):
        self.repository = repository

    def scorecard_inputs(self, start, end, outcome_window_h=24,
                         impression_id=None, experiments_only=False):
        repository = self.repository
        impressions = repository.scorecard_impressions(
            start, end, impression_id=impression_id, experiments_only=experiments_only)
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
        result = self.repository.served_recommendation_facts(impression_id)
        if result is None:
            return None
        inputs = (None if result['provenance'].get('policy_kind') == 'counterfactual' else
                  self.scorecard_inputs(result['served_at'], result['served_at'] + 1,
                                        impression_id=impression_id))
        return result, inputs

    def experiment_inputs(self, start, end, mature_end):
        inputs = self.scorecard_inputs(start, mature_end, experiments_only=True)
        return inputs, self.repository.experiment_shadows(start, end)

    def maintain_policy_history(self, now):
        return self.repository.maintain_policy_history(now)

    def publish_mix_menu(self, record, context):
        return self.repository.publish_mix_menu(record, context)

    def expand_mix_options(self, options):
        return self.repository.expand_mix_options(options)

    def policy_retention_candidates(self, now):
        return self.repository.policy_retention_candidates(now)

    def policy_retirement_pending(self, now):
        return self.repository.policy_retirement_pending(now)

    def restart_policy_retirement(self):
        return self.repository.restart_policy_retirement()

    def prune_policies(self, versions, cursor):
        return self.repository.prune_policies(versions, cursor)

    def mix_descriptor(self, descriptor_id):
        return self.repository.mix_descriptor(descriptor_id)

    def album_playback_context(self):
        return self.repository.album_playback_context()

    def remember_album_playback_context(self, value):
        return self.repository.remember_album_playback_context(value)

    def bind_album_playback_context(self, pending, account_id):
        return self.repository.bind_album_playback_context(pending, account_id)

    def has_served_request(self, request_id):
        return self.repository.has_served_request(request_id)

    def bind_choice_playback(self, account_id, playlist, video, opportunity, option, now, window_s):
        return self.repository.bind_choice_playback(account_id, playlist, video, opportunity, option, now, window_s)

    def choice_playback(self, account_id, playlist, video, now, window_s):
        return self.repository.choice_playback(account_id, playlist, video, now, window_s)

    def evidence(self):
        return self.repository.evidence()

    def publish_prepared_policies(self, rows, bases=()):
        return self.repository.publish_prepared_policies(rows, bases)

    def policy_configuration(self, version):
        return self.repository.policy_configuration(version)

    def record_serving_policy(self, version, configuration, now):
        return self.repository.record_serving_policy(version, configuration, now)

    def record_prepared_policies(self, rows, bases=()):
        return self.repository.record_prepared_policies(rows, bases)

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
        self.repository.insert_served_impressions(rows, policies=policies, bases=bases, versions=versions)
        if prune_before is not None:
            self._prune_impression_batch(prune_before)
            self._prune_response_batch(prune_before)
            self._prune_policy_batch(prune_before)

    def _prune_impression_batch(self, before):
        return self.repository._prune_impression_batch(before)

    def _prune_response_batch(self, before):
        return self.repository._prune_response_batch(before)

    def _prune_policy_batch(self, before):
        return self.repository._prune_policy_batch(before)

    def lane_impression_counts(self, since=None):
        return self.repository.lane_impression_counts(since)

    def maintain_impressions(self, now):
        return self.repository.maintain_impressions(now, before=now - RESPONSE_RETAIN_DAYS * 86400)

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
