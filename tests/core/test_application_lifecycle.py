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
    owners = ((lifecycle.policy_documents, 'policy'),
              (lifecycle.evidence_ledger, 'evidence'),
              (lifecycle.served_facts, 'served'),
              (lifecycle.collection_recovery, 'collection'),
              (lifecycle.group_documents, 'groups'),
              (lifecycle.recommendation_outputs, 'recommendations'),
              (lifecycle.metadata_delivery_files, 'metadata delivery'),
              (lifecycle.metadata_runtime_files, 'runtime metadata'),
              (lifecycle.legacy_evidence, 'legacy evidence'))
    for owner, name in owners:
        monkeypatch.setattr(owner, 'root_for_database', lambda path, key, name=name: name)
    monkeypatch.setattr(lifecycle.policy_documents, 'retained_reader', retained)
    session = SimpleNamespace(_db_path='library.db', _evidence_key='reader-key')
    with pytest.raises(ValueError, match='owner unavailable' if failure == 'admission' else 'consumer failed'):
        with lifecycle.ApplicationLifecycle.readers(session):
            raise ValueError('consumer failed')
    if failure == 'admission':
        assert events == [('enter', 'registered'), ('enter', 'policy'), ('enter', 'evidence'),
                          ('exit', 'policy'), ('exit', 'registered')]
    else:
        names = ('registered', *(name for _, name in owners))
        assert events == ([('enter', name) for name in names] +
                          [('exit', name) for name in reversed(names)])
