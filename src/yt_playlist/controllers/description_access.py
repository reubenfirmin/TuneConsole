"""Description consent, budget and availability over explicit access capabilities."""
from yt_playlist.controllers.access_configuration import clean
from yt_playlist.integrations.deepseek import credential_id
from yt_playlist.rec import paid_description_policy


class DescriptionAccess:
    def __init__(self, settings, requests, proposals, access, provider, *, paid, key_source=None):
        self.preferences, self.requests, self.proposals = settings, requests, proposals
        self.access, self.provider, self.paid = access, provider, paid
        self.key_source = key_source or access.credential_source('DEEPSEEK_API_KEY', 'deepseek_api_key')

    def key(self):
        return self.key_source()

    def availability(self, key, now):
        digest = credential_id(key)
        durable = (self.requests.availability(digest, now) if self.paid else
                   self.requests.legacy_availability(digest, now))
        return paid_description_policy.availability([row for row in
            (self.provider.availability(digest, now), durable) if row])

    def settings(self, now):
        key = self.key()
        usage = {} if self.paid else self.proposals.get_proposals('album_description_usage') or {}
        count = (self.requests.requests_today(now) if self.paid else
                 usage.get('requests', 0) if usage.get('day') == int(now // 86400) else 0)
        return paid_description_policy.configuration(key=key,
            saved=clean(self.preferences.get_setting('deepseek_api_key')),
            environment=clean(self.access.environment.get('DEEPSEEK_API_KEY')),
            enabled=self.preferences.get_setting('deepseek_album_descriptions_enabled', '1'),
            limit=self.preferences.get_setting('deepseek_daily_limit', str(paid_description_policy.DEFAULT_DAILY_LIMIT)),
            usage=usage, requests_today=count, wait=self.availability(key, now), now=now)
