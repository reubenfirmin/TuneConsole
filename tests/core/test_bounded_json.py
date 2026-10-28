"""Large cache reads retain standard JSON semantics and bounded container calls."""
import json

import pytest

from yt_playlist.core import bounded_json
from yt_playlist.core.activity import WorkBudgetExceeded


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(bounded_json, 'CHUNK_CHARS', 64)


@pytest.mark.parametrize('value', [None, True, False, 123, -1.23e-100, 'café 🎵', [], {},
    ['a"\\\n{}[],:', None, False, {'nested': [1, 2, {}]}],
    {'all': [{'key': str(n), 'text': 'café 🎵 \\"' * 20} for n in range(30)],
     'empty': {}, 'nested': {'array': [False, None, 1.23e100]}},
    'long scalar \\"' * 100,
])
def test_values_match_standard_decoder(value):
    for ascii_only in (True, False):
        encoded = ' \n' + json.dumps(value, ensure_ascii=ascii_only) + '\t' * 80
        decoded = bounded_json.loads(encoded)
        assert decoded == json.loads(encoded)
        assert json.dumps(decoded) == json.dumps(value)


@pytest.mark.parametrize('keys', [None, set(), {'all'}, {'missing'}, {'all', 'nested'}])
def test_selection_keeps_order_and_duplicate_key_semantics(keys):
    encoded = ('{"all":[1,2],"discard":' + json.dumps([{'x': 'text' * 50}] * 30)
               + ',"nested":{"a":[1]},"all":[3,4]}')
    value = json.loads(encoded)
    expected = value if keys is None else {key: item for key, item in value.items() if key in keys}
    assert json.dumps(bounded_json.loads(encoded, keys=keys)) == json.dumps(expected)


@pytest.mark.parametrize('invalid', ['[1,]', '{"a":1,}', '{"a" 1}', '{false:1}',
    '[1 2]', '{"a":}', '{"a":[1}', '[true', '"unterminated', '{"a":NaN,}',
    '{"a":1} extra', '[1] [2]', '{"a":"\\x00"}'])
@pytest.mark.parametrize('keys', [None, set()])
def test_malformed_values_fail_even_when_not_retained(invalid, keys):
    encoded = ' ' * 80 + invalid
    with pytest.raises(ValueError):
        bounded_json.loads(encoded, keys=keys)


def test_nonfinite_values_retain_existing_decoder_behavior():
    value = bounded_json.loads(' ' * 80 + '[NaN, Infinity, -Infinity]')
    assert json.dumps(value) == '[NaN, Infinity, -Infinity]'


def test_container_decoder_calls_are_bounded(monkeypatch):
    expected = [{'key': str(n), 'nested': {'x': [1, 2]}, 'text': 'long scalar' * 40}
                for n in range(50)]
    encoded = json.dumps(expected)
    decoder = json.JSONDecoder()
    calls = []

    class ObservedDecoder:
        def raw_decode(self, document, position=0):
            if document[position:position + 1] in ('[', '{'):
                calls.append(len(document) - position)
                assert len(document) - position <= bounded_json.CHUNK_CHARS
            return decoder.raw_decode(document, position)

    monkeypatch.setattr(bounded_json.json, 'JSONDecoder', ObservedDecoder)
    assert bounded_json.loads(encoded) == expected
    assert len(calls) > 50


@pytest.mark.parametrize('value', [[{'number': n} for n in range(100)], list(range(1000))])
def test_large_reads_can_be_cancelled_between_subtrees(monkeypatch, value):
    calls = []

    def cancel():
        calls.append(True)
        if len(calls) == 3:
            raise WorkBudgetExceeded('cancel between cache subtrees')

    monkeypatch.setattr(bounded_json, 'check_cancellable_work', cancel)
    with pytest.raises(WorkBudgetExceeded, match='between cache subtrees'):
        bounded_json.loads(json.dumps(value))
    assert len(calls) == 3
