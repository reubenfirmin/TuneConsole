"""AccessState/CredentialedAccessState: the narrow port IAOs receive instead of
a whole Store or its full EnrichmentState controller. Built from a bounded fake,
never a real Store -- no SQLite is opened anywhere in this file.
"""
from types import SimpleNamespace

from yt_playlist.integrations.base import AccessState, CredentialedAccessState


def fake_enrichment_state():
    calls = []

    class Fake:
        def integration_cooldown(self, provider):
            calls.append(('cooldown', provider))
            return {'retry_at': 42} if provider == 'known' else None

        def defer_integration(self, provider, *, now, retry_after, error):
            calls.append(('defer', provider, now, retry_after, error))
            return {'retry_at': retry_after}

        def clear_integration_cooldown(self, provider, admitted_deadline):
            calls.append(('clear', provider, admitted_deadline))
    return Fake(), calls


def test_access_state_exposes_exactly_the_three_cooldown_operations():
    enrichment_state, calls = fake_enrichment_state()
    state = AccessState(enrichment_state)
    assert state.integration_cooldown('known') == {'retry_at': 42}
    assert state.integration_cooldown('other') is None
    assert state.defer_integration('x', now=1, retry_after=2, error='e') == {'retry_at': 2}
    state.clear_integration_cooldown('x', 9)
    assert calls == [('cooldown', 'known'), ('cooldown', 'other'),
                      ('defer', 'x', 1, 2, 'e'), ('clear', 'x', 9)]


def test_access_state_for_store_is_none_for_no_store_and_narrows_otherwise():
    assert AccessState.for_store(None) is None
    enrichment_state, _ = fake_enrichment_state()
    store = SimpleNamespace(enrichment_state=enrichment_state, settings='SOME_UNRELATED_SETTINGS_OBJECT')
    state = AccessState.for_store(store)
    assert isinstance(state, AccessState)
    assert not hasattr(state, 'settings')  # The whole Store's other repos never leak through.


def test_credentialed_access_state_adds_only_settings_on_top():
    enrichment_state, calls = fake_enrichment_state()
    state = CredentialedAccessState(enrichment_state, lambda: 'secret')
    assert state.credential() == 'secret'
    assert not hasattr(state, 'settings')
    assert state.integration_cooldown('known') == {'retry_at': 42}
    assert calls == [('cooldown', 'known')]


def test_credentialed_access_state_for_store_carries_settings_through():
    enrichment_state, _ = fake_enrichment_state()
    store = SimpleNamespace(enrichment_state=enrichment_state,
                            settings=SimpleNamespace(get_setting=lambda name: 'VALUE'))
    state = CredentialedAccessState.for_store(store, lambda: 'VALUE')
    assert state.credential() == 'VALUE'
    assert CredentialedAccessState.for_store(None, lambda: None) is None
