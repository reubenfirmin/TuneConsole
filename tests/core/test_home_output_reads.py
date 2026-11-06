"""Immutable display reuse still detects damaged or unavailable owned files."""
from pathlib import Path

import pytest

from yt_playlist.core import fao, home_outputs, json_cache, recommendation_outputs as files


@pytest.mark.parametrize('surface,value', [
    ('home_prepared', 'A complete prepared menu'),
    ('radio_output', {'nested': [1]}),
])
def test_output_read_decodes_each_physical_read_once(tmp_path, monkeypatch, surface, value):
    filename, digest, size = files.publish(tmp_path, files.encode(surface, '0', value))
    decode, decoded = files._decode, []

    def counted(*args, **kwargs):
        decoded.append(True)
        return decode(*args, **kwargs)

    monkeypatch.setattr(files, '_decode', counted)
    first = files.read(filename, digest, size, surface=surface, chunk_key='0')
    assert first == value
    assert len(decoded) == 1
    if isinstance(first, dict):
        first['nested'].append(2)
    assert files.read(filename, digest, size, surface=surface, chunk_key='0') == value
    assert len(decoded) == (1 if isinstance(value, str) else 2)


@pytest.mark.parametrize('surface,chunk', [('home_choice', '0'), ('home_prepared', 'different')])
def test_output_read_rejects_a_different_owner_after_a_valid_cached_read(tmp_path, surface, chunk):
    filename, digest, size = files.publish(tmp_path, files.encode('home_prepared', '0', 'A prepared menu'))
    assert files.read(filename, digest, size, surface='home_prepared', chunk_key='0') == 'A prepared menu'
    with pytest.raises(ValueError, match='document or owner'):
        files.read(filename, digest, size, surface=surface, chunk_key=chunk)


def test_backup_read_keeps_validated_bytes_and_physical_age(tmp_path):
    encoded = files.encode('home_prepared', '0', 'A prepared menu')
    filename, digest, size = files.publish(tmp_path, encoded)
    restored, age = files._read(filename, digest, size, surface='home_prepared', chunk_key='0', with_age=True)
    assert restored == encoded and age.logical_bytes == size
    with pytest.raises(ValueError, match='document or owner'):
        files._read(filename, digest, size, surface='home_choice', chunk_key='0', with_age=True)


@pytest.mark.parametrize('change', ['replace', 'tamper', 'delete'])
def test_home_part_reuse_rechecks_the_physical_file(tmp_path, monkeypatch, change):
    encoded = files.encode('home_prepared', '0', 'A complete prepared menu')
    filename, digest, size = files.publish(tmp_path, encoded)
    original = files._read
    reads = []
    def counted(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(files, '_read', counted)
    def read():
        return files.read(filename, digest, size, surface='home_prepared', chunk_key='0')
    assert read() == read() == 'A complete prepared menu'
    assert len(reads) == 1
    if change == 'replace':
        fao.write(filename, encoded)
        assert read() == 'A complete prepared menu'
        assert len(reads) == 2
    elif change == 'tamper':
        fao.write(filename, files.encode('home_prepared', '0', 'A different menu'))
        with pytest.raises(ValueError):
            read()
    else:
        Path(filename).unlink()
        with pytest.raises(FileNotFoundError):
            read()
    with pytest.raises(ValueError, match='byte range'):
        files.read(filename, digest, False, surface='home_prepared', chunk_key='0')


def test_home_part_reuse_has_a_total_byte_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(files, '_HOME_CACHE_BYTES', 100)
    for index in range(4):
        filename, digest, size = files.publish(tmp_path, files.encode('home_tuned', str(index), 'x' * 60))
        assert files.read(filename, digest, size, surface='home_tuned', chunk_key=str(index)) == 'x' * 60
        assert sum(item[1] for item in files._home_parts.values()) <= 100


@pytest.mark.parametrize('surface', ['home_tuned', 'home_choice'])
def test_explicit_selection_uses_larger_bounded_parts_without_changing_the_document(surface):
    import json
    raw = json.dumps({'tracks': ['東京' * 40000]}, ensure_ascii=False)
    prepared = home_outputs.prepare(surface, raw, 1000)
    assert len(prepared.parts) == 1
    assert all(len(part) <= files.MAX_BYTES for part in prepared.parts)
    assert files._decode(prepared.parts[0], surface, '0')['output'] == raw


@pytest.mark.parametrize('surface', ['home_tuned', 'home_choice'])
def test_ascii_selection_parts_round_trip_escaping_and_multiple_files(tmp_path, surface):
    import json
    raw = json.dumps({'tracks': ['"\\\n\t' * 90000]})
    prepared = home_outputs.prepare(surface, raw, 1000)
    assert len(prepared.parts) > 1
    assert len(prepared.parts) < (len(raw) + home_outputs.SELECTION_PART_CHARACTERS - 1) // home_outputs.SELECTION_PART_CHARACTERS
    restored = []
    for position, encoded in enumerate(prepared.parts):
        assert len(encoded) <= files.MAX_BYTES
        filename, digest, size = files.publish(tmp_path, encoded)
        restored.append(files.read(filename, digest, size, surface=surface, chunk_key=str(position)))
    assert ''.join(restored) == raw
    assert json.loads(''.join(restored)) == prepared.value


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_legacy_compressed_selection_rejects_nonfinite_nested_inputs(value):
    raw = json_cache.encode({'tracks': [{'musical': {'feature': value, 'other': 'x' * 100000}}]},
                            shared_profiles=False)
    with pytest.raises(ValueError, match='Non-finite'):
        home_outputs.prepare('home_tuned', raw, 1000)
