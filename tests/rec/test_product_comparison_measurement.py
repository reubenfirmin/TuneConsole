"""Comparison policy consumes evidence records without opening a database.

Assignments here are fixed analytics inputs. Actual day assignment, durable
capture, corrections, retention and serving remain in test_product_comparison.
"""
import sqlite3
from unittest.mock import Mock

import pytest

from yt_playlist.rec import product_comparison as product


@pytest.fixture
def measurement(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Comparison measurement opened SQLite outside its repository')

    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    records = {'opportunity': [], 'response': []}
    evidence = Mock(spec_set=['records'])
    evidence.records.side_effect = records.__getitem__
    store = Mock(spec_set=['rec_ledger'])
    store.rec_ledger = Mock(spec_set=['evidence'])
    store.rec_ledger.evidence.return_value = evidence
    current = {'run_id': 17, 'generation': 'frozen-serving',
               'policy': dict(product.POLICY), 'alpha': .025, 'started_at': 0,
               'exposure_until': 28 * product.DAY, 'decision_at': 29 * product.DAY}
    return store, current, records


def offer(records, current, day, *, format='mixes', episode='episode',
          outcome=1, suffix=''):
    """Literal repository DTOs, with fourteen assigned days for each arm."""
    at = day * product.DAY + 100
    arm = 'challenger' if day % 2 else 'incumbent'
    identity = f'{format}:{day}:{episode}:{suffix}'
    records['opportunity'].append({
        'schema': 1, 'id': identity, 'format': format, 'at': at, 'observed_at': at,
        'episode_id': f'{day}:{episode}', 'context': None, 'prediction': None,
        'options': [{'id': 'chosen', 'score': .7}],
        'comparison': {'run_id': current['run_id'], 'arm': arm, 'probability': .5,
                       'unit': str(day), 'format': format, 'window': f'{day}:{suffix}',
                       'policy': current['policy']['version'],
                       'generation': current['generation'], 'at': at, 'controlled': True},
    })

    def response(kind, offset, *, option=None, visible=(), details=None):
        records['response'].append({
            'schema': 1, 'id': f'{identity}:{kind}:{option or ""}',
            'opportunity_id': identity, 'kind': kind, 'option_id': option,
            'at': at + offset, 'observed_at': at + offset,
            'visible': list(visible), 'details': details or {},
        })

    response('visible', 1, visible=['chosen'])
    if outcome == 'close':
        response('close', 2)
    else:
        response('choice', 2, option='chosen')
        response('start', 3, option='chosen')
        if outcome is not None:
            response('experience', 4, option='chosen', details={
                'kind': 'track_exit', 'position': 200 if outcome else 20, 'duration': 200})


def test_many_tracks_or_branches_cannot_manufacture_independent_days(measurement):
    store, current, records = measurement
    for index in range(40):
        offer(records, current, 0, suffix=str(index))
    report = product.measure(store, current, current['decision_at'])
    assert sum(report['overall']['days'].values()) == 1
    assert sum(len(values) for values in report['overall']['samples'].values()) == 1
    assert product.decision(current, report, current['decision_at'])[0] == 'retained'


@pytest.mark.parametrize('format', product.FORMATS)
def test_a_single_listening_format_can_supply_the_complete_comparison(measurement, format):
    store, current, records = measurement
    for day in range(28):
        for episode in range(2):
            offer(records, current, day, format=format, episode=str(episode),
                  outcome=1 if day % 2 else 'close')
    report = product.measure(store, current, current['decision_at'])
    assert sum(report['overall']['days'].values()) == 28
    assert sum(row['observed'] for row in report['formats'][format]['arms'].values()) == 56
    offers = records['opportunity']
    assert len(offers) == 56 and {row['format'] for row in offers} == {format}
    assert product.decision(current, report, current['decision_at'])[0] == 'applied', report


def test_format_regression_is_not_hidden_by_other_formats(measurement):
    store, current, records = measurement
    for day in range(28):
        for format in product.FORMATS:
            success = bool(day % 2) != (format == 'radio')
            offer(records, current, day, format=format, outcome=1 if success else 'close')
    report = product.measure(store, current, current['decision_at'])
    assert report['overall']['difference'] == pytest.approx(1 / 3)
    assert report['formats']['radio']['difference'] == -1
    assert product.decision(current, report, current['decision_at'])[0] == 'retained'


def test_missing_endings_can_reverse_an_apparent_gain_without_becoming_negative_votes(measurement):
    store, current, records = measurement
    for day in range(28):
        for episode in range(10):
            offer(records, current, day, episode=str(episode),
                  outcome=None if episode >= 7 else 1 if day % 2 else 'close')
    report = product.measure(store, current, current['decision_at'])
    assert sum(report['overall']['days'].values()) == 28
    assert sum(row['mature'] for row in report['formats']['mixes']['arms'].values()) == 280
    assert report['overall']['difference'] == 1
    assert report['overall']['missing_outcome_bounds']['difference_bounds'] == pytest.approx([.4, 1])
    assert sum(row['arms'][arm].get('unknown', 0) for row in report['formats'].values()
               for arm in ('incumbent', 'challenger')) == 84
    assert product.decision(current, report, current['decision_at'])[0] == 'retained'
