"""Description consent, budget and availability over explicit access capabilities."""
from yt_playlist.controllers.access_configuration import clean
from yt_playlist.integrations.deepseek import credential_id
from yt_playlist.rec import paid_description_policy


class DescriptionAccess:
    def __init__(self, settings, requests, access, provider, *, key_source=None):
        self.preferences, self.requests = settings, requests
        self.access, self.provider = access, provider
        self.key_source = key_source or access.credential_source('DEEPSEEK_API_KEY', 'deepseek_api_key')

    def key(self):
        return self.key_source()

    def availability(self, key, now):
        digest = credential_id(key)
        durable = self.requests.availability(digest, now)
        return paid_description_policy.availability([row for row in
            (self.provider.availability(digest, now), durable) if row])

    def settings(self, now):
        key = self.key()
        return paid_description_policy.configuration(key=key,
            saved=clean(self.preferences.get_setting('deepseek_api_key')),
            environment=clean(self.access.environment.get('DEEPSEEK_API_KEY')),
            enabled=self.preferences.get_setting('deepseek_album_descriptions_enabled', '1'),
            limit=self.preferences.get_setting('deepseek_daily_limit', str(paid_description_policy.DEFAULT_DAILY_LIMIT)),
            requests_today=self.requests.requests_today(now), wait=self.availability(key, now))
