"""Enrichment provider config: the ordered, enable/disable list the Setup → Enrichment tab edits.

This is *config only*: it records the order the user wants metadata providers run in, and which are
enabled. A future background worker consumes this; nothing here runs a provider. `load_config` is the
canonical reader: it always returns a valid, fully-populated, dependency-respecting list, so callers
(the worker, the template) never have to cope with missing/corrupt/partial saved state.

Ordering constraint is data-driven via each provider's `requires`: a provider must appear *after* the
one it requires (AcousticBrainz is keyed by MusicBrainz recording MBIDs, so it follows MusicBrainz).
"""
from yt_playlist.providers import enrichment_policy
from yt_playlist.providers.enrichment_policy import (
    PROVIDERS, GENRE_POLICY_VERSION, normalize_config, serialize_config,
    repair_order as _repair_order,
)
from yt_playlist.util import genre_catalog

_SETTING_KEY = enrichment_policy.SETTING_KEY


def provider_configuration(store, registry, config=None):
    """Effective provider availability and settings, without retaining credentials in coverage."""
    from yt_playlist.controllers.enrichment_configuration import provider_configuration as acquire
    return acquire(store, registry, config, policy_version=GENRE_POLICY_VERSION)


def configuration_fingerprints(config, facts, vocabulary):
    """Detached coverage policy; credentials leave this function only as digests."""
    return enrichment_policy.configuration_fingerprints(
        config, facts, vocabulary, policy_version=GENRE_POLICY_VERSION)


def load_config(store):
    """The canonical, always-valid provider config: an ordered list of dicts merged from the registry,
    each `{name, label, blurb, key, requires, enabled}`. Unknown saved names are dropped; providers
    absent from saved state are appended (enabled); ordering is repaired to respect `requires`."""
    return normalize_config(store.settings.get_setting(_SETTING_KEY))


def save_config(store, items):
    """Persist an ordered list of `{name, enabled}`. Raises ValueError if a name is unknown or the
    order violates a `requires` constraint (a provider before its prerequisite)."""
    store.settings.set_setting(_SETTING_KEY, serialize_config(items))
