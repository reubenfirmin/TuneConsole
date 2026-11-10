"""Native evidence keeps source scope, bounded history and current dependencies."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_metadata_evidence_migration import additive_catalog, catalog
from yt_playlist.core import metadata_native
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.metadata_observations import MetadataObservation


def _entry(store):
    return store.tracks.upsert_track('video', 'Song', 'Artist', None, 200)


def _observation(value=.2, **changes):
    return replace(MetadataObservation('mood_party', value, 'recording', 'source-recording', 'prediction',
                                      unit='classifier_probability'), **changes)


def _record(store, entry, observations, run='one', provider='native'):
    store.enrichment_state.record_metadata_observations(entry, provider, observations, run_id=run, now=20)


def test_native_delivery_replay_preserves_history_and_does_not_roll_current_source_back(catalog):
    store, entry = catalog, _entry(catalog)
    first = _observation()
    _record(store, entry, [first])
    _record(store, entry, [first])
    _record(store, entry, [_observation(.8)], run='two')
    selected = list(map(tuple, store.conn.execute('SELECT * FROM musical_source_observations')))
    revision = store.conn.execute('SELECT revision FROM rec_musical_sources').fetchone()[0]
    _record(store, entry, [first])
    assert list(map(tuple, store.conn.execute('SELECT * FROM musical_source_observations'))) == selected
    assert store.conn.execute('SELECT revision FROM rec_musical_sources').fetchone()[0] == revision
    history = store.enrichment_state.metadata_observations('song|artist')
    assert [row['value'] for row in history] == [.2, .8]
    assert [row['provenance']['delivery_id'] for row in history] == ['one', 'two']
    key = store.musical_profiles.pending()
    source, _, _ = store.musical_profiles.source_input(key)
    assert source['observations']['mood_party']['value'] == .8
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='metadata_findings'").fetchone()
    with pytest.raises(WorkBudgetExceeded):
        store.musical_profiles.source_input(key, max_bytes=1)
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        _record(store, entry, [_observation(.9)], run='two')
    assert store.enrichment_state.metadata_observations('song|artist') == history


def test_native_source_scopes_remain_distinct_and_repeated_field_observations_page(catalog):
    store, entry = catalog, _entry(catalog)
    tag = _observation([{'name': 'Jazz', 'count': 7}], field='tags', kind='reported', unit='provider_tag_count_or_order')
    observations = [replace(tag, scope=scope, entity_id=scope)
                    for scope in ('recording', 'release', 'artist', 'genre', 'item')]
    _record(store, entry, observations)
    _record(store, entry, [_observation(.1), _observation(.3), _observation(.5)], run='repeated')
    first = store.enrichment_state.metadata_observations('song|artist', limit=6)
    second = store.enrichment_state.metadata_observations('song|artist', after_id=first[-1]['id'], limit=6)
    assert [row['provenance']['scope'] for row in first[:5]] == ['recording', 'release', 'artist', 'genre', 'item']
    assert [row['value'] for row in first[5:] + second] == [.1, .3, .5]
    assert store.enrichment_state.metadata_observations('song|artist', after_id=second[-1]['id']) == []
    assert store.conn.execute('SELECT count(*) FROM rec_musical_sources').fetchone()[0] == 4
    assert store.conn.execute('SELECT genre FROM recordings').fetchone()[0] is None
    assert store.conn.execute('SELECT mood_happy FROM recordings').fetchone()[0] is None
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_native_source_selection_rejects_a_different_provider_entity(catalog):
    store, entry = catalog, _entry(catalog)
    _record(store, entry, [_observation()])
    _record(store, entry, [_observation(entity_id='other-recording')], run='other')
    key = json.dumps(['native', 'recording', 'source-recording'], separators=(',', ':'))
    other = store.conn.execute('SELECT max(id) FROM native_metadata_observations').fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='different provider entity'):
        with store.conn:
            store.conn.execute('UPDATE musical_source_observations SET observation_id=? WHERE source_key=?', (other, key))


def test_interrupted_native_publication_preserves_selection_and_reuses_the_committed_delivery(catalog, monkeypatch):
    store, entry = catalog, _entry(catalog)
    _record(store, entry, [_observation()])
    tables = ('native_metadata_observations', 'musical_source_observations', 'rec_musical_sources', 'rec_musical_links')
    before = {table: list(map(tuple, store.conn.execute('SELECT * FROM ' + table))) for table in tables}
    deliveries = store.conn.execute('SELECT COUNT(*) FROM metadata_deliveries').fetchone()[0]
    repository = store.enrichment_state.repository
    publish = repository.store_native_observation

    def interrupt(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError('interrupted native source publication')

    monkeypatch.setattr(repository, 'store_native_observation', interrupt)
    with pytest.raises(RuntimeError, match='interrupted native'):
        _record(store, entry, [_observation(.7)], run='interrupted')
    assert {table: list(map(tuple, store.conn.execute('SELECT * FROM ' + table))) for table in tables} == before
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_deliveries').fetchone()[0] == deliveries + 1
    assert [row['value'] for row in store.enrichment_state.metadata_observations('song|artist')] == [.2]
    monkeypatch.setattr(repository, 'store_native_observation', publish)
    _record(store, entry, [_observation(.7)], run='interrupted')
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_deliveries').fetchone()[0] == deliveries + 1
    assert [row['value'] for row in store.enrichment_state.metadata_observations('song|artist')] == [.2, .7]


@pytest.mark.parametrize('unit', [True, 42, {}, '', 'x' * 257])
def test_native_unit_is_validated_before_publishing_delivery_or_history(catalog, unit):
    store, entry = catalog, _entry(catalog)
    before = list(store.conn.iterdump())
    with pytest.raises(ValueError, match='unit must be bounded text'):
        _record(store, entry, [_observation(unit=unit)])
    assert list(store.conn.iterdump()) == before
