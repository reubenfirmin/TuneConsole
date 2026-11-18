"""Enrichment configuration commands and coherent provider/vocabulary snapshots."""
from yt_playlist.providers import enrichment_policy as enrichment
from yt_playlist.util import genre_matching as genres


def provider_configuration(store, registry, config=None, *, policy_version=enrichment.GENRE_POLICY_VERSION):
    config = config if config is not None else enrichment.normalize_config(
        store.settings.get_setting(enrichment.SETTING_KEY))
    facts = {}
    for item in config:
        name = item['name']
        provider = registry.get(name)
        if provider is None:
            continue
        credential = (provider.api_key(store) if name == 'lastfm' and hasattr(provider, 'api_key') else
                      provider.token(store) if name == 'discogs' and hasattr(provider, 'token') else '')
        facts[name] = {'available': provider.available(store) if item['enabled'] else False,
                       'credential': credential}
    return enrichment.configuration_fingerprints(config, facts, store.genres.get_genre_whitelist(),
                                                policy_version=policy_version)


class EnrichmentConfiguration:
    def __init__(self, settings, vocabulary, *, wake=lambda: None):
        self.settings, self.vocabulary, self.wake = settings, vocabulary, wake

    def query(self):
        return enrichment.normalize_config(self.settings.get_setting(enrichment.SETTING_KEY))

    def save(self, items):
        content = enrichment.serialize_config(items)
        self.settings.set_setting(enrichment.SETTING_KEY, content)
        self.wake()

    def lastfm_key(self, value):
        self.settings.set_setting('lastfm_api_key', value.strip())
        self.wake()

    def matcher(self):
        matcher = self.vocabulary.enabled_matcher()
        if matcher is None:
            return genres.GenreMatcher.from_values(genres.build_canon(self.vocabulary.get_genre_whitelist()))
        from yt_playlist.util import genre_catalog
        keys = {genre_catalog.normalize(row['name']): row['key'] for row in self.vocabulary.taxonomy()['genres']}
        return genres.GenreMatcher.from_values(matcher, keys)
