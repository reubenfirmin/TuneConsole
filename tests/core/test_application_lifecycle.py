"""Retained application owners are admitted before use and unwound on failure."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.library import application_lifecycle as lifecycle

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('failure', ['admission', 'consumer'])
def test_retained_owner_failure_releases_already_admitted_lifetimes(monkeypatch, failure):
    events = []

    @contextmanager
    def retained(name):
        events.append(('enter', name))
        if failure == 'admission' and name == 'evidence':
            raise ValueError('owner unavailable')
        try:
            yield
        finally:
            events.append(('exit', name))

    def roots(path, key):
        assert (path, key) == ('library.db', 'reader-key')
        return retained('registered')

    monkeypatch.setattr(lifecycle.file_owners, 'readers', roots)
    for owner, name in ((lifecycle.policy_documents, 'policy'),
                        (lifecycle.evidence_ledger, 'evidence'),
                        (lifecycle.served_facts, 'served')):
        monkeypatch.setattr(owner, 'root_for_database', lambda path, key, name=name: name)
    monkeypatch.setattr(lifecycle.policy_documents, 'retained_reader', retained)
    session = SimpleNamespace(_db_path='library.db', _evidence_key='reader-key')
    with pytest.raises(ValueError):
        with lifecycle.ApplicationLifecycle.readers(session):
            assert events == [('enter', name) for name in ('registered', 'policy', 'evidence', 'served')]
            raise ValueError('consumer failed')
    exits = [name for phase, name in events if phase == 'exit']
    assert exits == (['policy', 'registered'] if failure == 'admission' else
                     ['served', 'evidence', 'policy', 'registered'])
