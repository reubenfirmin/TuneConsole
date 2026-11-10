"""Source-integrity regressions for external human annotations and ZIP ranges."""
import csv
import io
import json
import urllib.error

import pytest

from experiments.audio_lab_tools.datasets import dataset_audit as audit
from experiments.audio_lab_tools.datasets import dataset_range as ranges
from experiments.audio_lab_tools.support.external_experiment import save


def csv_file(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_human_windows_preserve_signed_values_and_equal_listener_weight(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'DATA', tmp_path)
    root = tmp_path / 'discovery/manymusic'
    base = root / 'metadata/manymusic-stim-zenodo'
    csv_file(base / 'manymusic-dataset/meta/manymusic_mtg_jamendo_tags.csv',
             ['track_id', 'artist_id'], [{'track_id': 'one', 'artist_id': 'artist'}])
    csv_file(base / 'validation-dataset/meta/suno-v4.5_effnet-discogs400.csv', ['trackId', 'prompt_id'], [])
    fields = ['subjId', 'source', 'trackId', 'trackStartSec', 'trackPlaySec', 'perceived_arousal', 'perceived_valence']
    common = dict(source='Jamendo-A', trackId='one', trackStartSec='15', trackPlaySec='30')
    rows = [{**common, 'subjId': subject, 'perceived_arousal': value, 'perceived_valence': value}
            for subject, value in [('s1', '-1'), ('s1', '1'), ('s2', '1')]]
    rows += [{**rows[0], 'source': 'Jamendo-B', 'trackStartSec': '45'},
             {**rows[0], 'source': 'AppleMusic', 'trackId': 'missing'}]
    csv_file(base / 'validation-dataset/ratings/ratings.csv', fields, rows)
    save(root / 'archive-members.json', {'members': [{'name': 'audio/one.mp3'}]})
    result = audit.many_music()
    assert result['recordings'] == 1
    assert result['windows'] == 2
    first, second = result['rows']
    assert first['targets'] == [.5, .5]  # s1's two responses contribute one mean vote.
    assert second['targets'] == [-1., -1.]
    assert first['rater_count'] == 2
    assert first['group'] == second['group']
    assert first['native_scale'] == [-1, 1]
    assert result['repeated_listener_window_responses'] == 1
    assert result['conflicting_listener_windows'] == 1
    assert result['excluded_missing_audio_responses'] == {'AppleMusic': 1}


class Response(io.BytesIO):
    status = 206

    def __init__(self, data, content_range):
        super().__init__(data)
        self.headers = {'Content-Range': content_range}


def test_range_reader_rejects_wrong_ranges_and_changed_cached_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(ranges.urllib.request, 'urlopen', lambda *a, **k: Response(b'abcd', 'bytes 1-4/10'))
    reader = ranges.RemoteZip('https://source.test/audio.zip', 10, tmp_path, budget=8)
    with pytest.raises(ValueError, match='exact archive range'):
        reader.read(4)
    monkeypatch.setattr(ranges.urllib.request, 'urlopen', lambda *a, **k: Response(b'abcd', 'bytes 0-3/10'))
    reader = ranges.RemoteZip('https://source.test/audio.zip', 10, tmp_path, budget=8)
    assert reader.read(4) == b'abcd'
    (tmp_path / 'ranges/0-3.bin').write_bytes(b'bad!')
    reader.seek(0)
    with pytest.raises(ValueError, match='Cached archive range changed'):
        reader.read(4)


def test_failed_range_attempts_count_toward_request_budget(tmp_path, monkeypatch):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.URLError('temporary reset')

    monkeypatch.setattr(ranges.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(ranges.time, 'sleep', lambda _: None)
    reader = ranges.RemoteZip('https://source.test/audio.zip', 10, tmp_path, budget=8)
    with pytest.raises(ValueError, match='request-byte budget'):
        reader.read(4)
    assert len(attempts) == 2
    assert reader.requested == 8


def test_publisher_attribution_requires_matching_ids_and_retains_ambiguous_names():
    first = ('07/7.mp3\nSong by Artist from Jamendo: http://www.jamendo.com/track/7\n'
             'Available under a Creative Commons license: http://creativecommons.org/licenses/by/3.0/')
    second = first.replace('07/7.mp3', '08/8.mp3').replace('/track/7', '/track/8').replace(
        'Song by Artist', 'Song by Sea by Night')
    result = audit.many_music_credits(first + '\n\n' + second, {'7', '8'})
    assert result['7']['artist'] == 'Artist'
    assert result['8']['artist'] == ''
    assert result['8']['publisher_attribution'].startswith('Song by Sea by Night')
    assert result['7']['license_url'] == 'http://creativecommons.org/licenses/by/3.0/'
    with pytest.raises(ValueError, match='inconsistent identity'):
        audit.many_music_credits(first.replace('/track/7', '/track/9'), {'7'})
    with pytest.raises(ValueError, match='Missing selected'):
        audit.many_music_credits(first, {'7', '8'})
    with pytest.raises(ValueError, match='Duplicate'):
        audit.many_music_credits(first + '\n\n' + first, {'7'})
