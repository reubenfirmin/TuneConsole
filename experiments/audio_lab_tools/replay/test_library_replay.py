"""Check the consumer adapter's observation and missing-identity boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from experiments.audio_lab_tools.replay.library_replay import compare_outputs, consumer_adapter
from experiments.audio_lab_tools.replay.test_external_propagation import fixture_library
from experiments.audio_lab_tools.replay.external_propagation import current_inputs
from yt_playlist.rec import musical_character, musical_profiles
from experiments.audio_lab_tools.replay import replay_runtime
from experiments.audio_lab_tools.replay import paired_campaign
from experiments.audio_lab_tools.replay import replay_storage
from experiments.audio_lab_tools.replay import replay_followups


def test_consumer_adapter_reuses_only_original_votes_and_preserves_unknown_profiles():
    library = fixture_library()
    library['recordings'] = [{'id': key, 'identity_key': str(key)} for key in (10, 11, 12)]
    metadata, observations = current_inputs(library, {'provider_measurement'})
    observations[10]['fits']['energize'] = {'score': .85, 'support': 1.}
    profiles = {str(key): musical_profiles.resolve([], genre=None) for key in (10, 11, 12, 99)}
    profiles['11']['dimensions']['intensity'] = {'value': .3, 'support': 1., 'override': True}
    untouched = deepcopy(profiles)
    adapted, calls = consumer_adapter(lambda _store, _keys: profiles, library, metadata, observations)
    result = adapted(None, list(profiles))
    assert result['10']['vibes']['fits']['relax']['score'] == .9
    assert result['12']['vibes']['fits']['relax']['score'] == pytest.approx(.66)
    assert result['12']['vibes']['fits']['relax']['recordings'] == 1
    assert musical_character.intensity({'musical': result['10']}) == .85
    assert musical_character.intensity({'musical': result['11']}) == .3
    assert musical_character.intensity_evidence({'musical': result['99']}) == (None, 0)
    assert result['99'] == untouched['99']
    assert profiles == untouched
    assert calls['adapted_reads'] == 3
    assert calls['missing_frozen_identity_reads'] == 1


def frozen_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_runtime, 'WORK', tmp_path)
    root = tmp_path / 'runtime'
    source = root / 'src/yt_playlist/example.py'
    source.parent.mkdir(parents=True)
    source.write_text('VALUE = 1\n')
    manifest = {'files': {'src/yt_playlist/example.py': replay_runtime.sha256(source)}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root, source


def test_frozen_replay_runtime_rejects_changed_and_untracked_source(tmp_path, monkeypatch):
    root, source = frozen_runtime(tmp_path, monkeypatch)
    replay_runtime.verify(root)
    original = source.read_text()
    source.write_text('VALUE = 2\n')
    with pytest.raises(ValueError, match='runtime changed'):
        replay_runtime.verify(root)
    source.write_text(original)
    source.with_name('unexpected.py').write_text('VALUE = 3\n')
    with pytest.raises(ValueError, match='unexpected or missing'):
        replay_runtime.verify(root)


def test_frozen_replay_runtime_rejects_mutable_application_import(tmp_path, monkeypatch):
    root, _source = frozen_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv(replay_runtime.ENVIRONMENT, str(root))
    # This test process has imported the actual checkout package above. A replay
    # claiming to use the private runtime must reject that mixed provenance.
    with pytest.raises(ValueError, match='imported mutable production source'):
        replay_runtime.runtime_hashes()


@pytest.mark.parametrize('field', ['models', 'runtime_code_sha256', 'code_sha256',
                                 'input_manifest_sha256', 'runtime_environment', 'publication_limit_s', 'choices',
                                 'execution_mode'])
def test_paired_resume_rejects_changed_provenance(field):
    plan = {field: {'original': True}}
    paired_campaign.validate_plan(plan, deepcopy(plan))
    with pytest.raises(ValueError, match='provenance changed: ' + field):
        paired_campaign.validate_plan(plan, {field: {'changed': True}})


def consumer_receipt(tmp_path):
    output = tmp_path / 'checkpoint/attempt-1'
    output.mkdir(parents=True)
    raw = output / 'candidate-radio.json'
    raw.write_text('{"0.5": []}\n')
    result = {'output': str(output.relative_to(tmp_path)),
              'output_sha256': {str(raw.relative_to(tmp_path)): replay_runtime.sha256(raw)}}
    receipt = output / 'result.json'
    receipt.write_text(json.dumps(result))
    result['receipt_sha256'] = replay_runtime.sha256(receipt)
    return output, raw, result


def test_paired_resume_rejects_changed_output_and_untracked_files(tmp_path):
    output, raw, result = consumer_receipt(tmp_path)
    paired_campaign.verify_receipts(tmp_path, {'checkpoint': result})
    original = raw.read_text()
    raw.write_text('{"changed": true}')
    with pytest.raises(ValueError, match='output changed'):
        paired_campaign.verify_receipts(tmp_path, {'checkpoint': result})
    raw.write_text(original)
    (output / 'unexpected.json').write_text('{}')
    with pytest.raises(ValueError, match='file set changed'):
        paired_campaign.verify_receipts(tmp_path, {'checkpoint': result})


def test_paired_resume_rejects_changed_summary_and_path_escape(tmp_path):
    _output, _raw, result = consumer_receipt(tmp_path)
    changed = {**result, 'invented_result': True}
    with pytest.raises(ValueError, match='receipt changed'):
        paired_campaign.verify_receipts(tmp_path, {'checkpoint': changed})
    with pytest.raises(ValueError, match='escapes its campaign'):
        paired_campaign.verify_receipts(tmp_path, {'checkpoint': {**result, 'output': '../outside'}})


def test_consumer_comparison_keeps_unknown_intensity_distinct_from_known_midpoint(tmp_path):
    output = tmp_path / 'checkpoint'
    output.mkdir()
    tracks = [{'key': 'known', 'musical': {'dimensions': {'intensity': {'value': .5, 'support': .8}}}},
              {'key': 'unknown', 'musical': {}}]
    (output / 'candidate-mixes.json').write_text(json.dumps({
        'relax': [{'lane': 'wheelhouse', 'tracks': tracks}]}))
    (output / 'candidate-radio.json').write_text('{}')
    result = compare_outputs(tmp_path, ['checkpoint'])['checkpoint']['mix:relax:wheelhouse']
    assert result['mean_shared_intensity'] == .5
    coverage = result['intensity_coverage']
    assert coverage['known_tracks'] == coverage['unknown_tracks'] == 1
    assert coverage['mean_known_intensity'] == .5
    assert coverage['selected_input_sources'] == {'profile_dimension': 1, 'unknown': 1}
    assert coverage['selected_vibe_inputs'] == {}
    assert coverage['known_adjacent_pairs'] == 0
    assert coverage['mean_known_adjacent_intensity_change'] is None


def test_fresh_metadata_reads_only_selected_identities_without_changing_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_followups.library_replay, 'WORK', tmp_path)
    database = tmp_path / 'source.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE catalog_tracks(id INTEGER,identity_key TEXT,genre TEXT,mb_year TEXT)')
        conn.executemany('INSERT INTO catalog_tracks VALUES(?,?,?,?)', [
            (1, 'selected', 'house', '1996'), (2, 'unselected', 'rock', '2001')])
    checksum = replay_runtime.sha256(database)
    (tmp_path / 'plan.json').write_text(json.dumps({'frozen_database_sha256': checksum}))
    rows, source_hash = replay_followups.snapshot_metadata(database, {'selected', 'absent'})
    assert rows == {'selected': {'genre': 'house', 'year': '1996'}}
    assert source_hash == replay_runtime.sha256(database) == checksum
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE catalog_tracks SET genre='pop' WHERE identity_key='selected'")
    with pytest.raises(ValueError, match='Frozen metadata database changed'):
        replay_followups.snapshot_metadata(database, {'selected'})


def test_fresh_metadata_rejects_database_outside_experiment_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_followups.library_replay, 'WORK', tmp_path / 'private')
    with pytest.raises(ValueError, match='experiments-only SQL snapshot'):
        replay_followups.snapshot_metadata(tmp_path / 'live.db', set())


def private_payload_fixture(tmp_path, monkeypatch):
    from yt_playlist.core import policy_documents, recommendation_outputs
    work = tmp_path / 'campaigns'
    monkeypatch.setattr(replay_storage, 'WORK', work)
    campaign = work / 'paired-slices-fixture'
    campaign.mkdir(parents=True)
    database = campaign / 'source.db'
    with sqlite3.connect(database) as conn:
        for owner in policy_documents.OWNERS:
            conn.execute(f'CREATE TABLE {owner}(version TEXT PRIMARY KEY,configuration_filename TEXT,'
                         'content_digest TEXT,configuration_offset INTEGER,byte_size INTEGER)')
            for index, (filename, checksum, size, offset) in enumerate(policy_documents.publish_many(
                    tmp_path / 'original-policies', [{'setting': 1}, {'setting': 2}])):
                conn.execute(f'INSERT INTO {owner} VALUES(?,?,?,?,?)',
                             (str(index), filename, checksum, offset, size))
        conn.execute('CREATE TABLE prepared_outputs(id INTEGER PRIMARY KEY,surface TEXT,chunk_key TEXT,'
                     'filename TEXT,content_digest TEXT,byte_size INTEGER)')
        filename, checksum, size = recommendation_outputs.publish(tmp_path / 'original-recommendations',
            recommendation_outputs.encode('radio', 'framing', {'tracks': ['a', 'b']}))
        conn.execute('INSERT INTO prepared_outputs VALUES(1,?,?,?,?,?)',
                     ('radio', 'framing', filename, checksum, size))
    return database, campaign


def test_consumer_payload_copy_survives_original_collection_and_enforces_read_boundary(tmp_path, monkeypatch):
    from yt_playlist.core import policy_documents, recommendation_outputs
    database, campaign = private_payload_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(database) as conn:
        before = replay_storage.owned_rows(conn)
    source_hashes = {row['filename']: replay_runtime.sha256(row['filename']) for row in before}
    report = replay_storage.freeze(database, campaign)
    with sqlite3.connect(database) as conn:
        after = replay_storage.owned_rows(conn)
    assert report['records'] == 5
    assert [{key: value for key, value in row.items() if key != 'filename'} for row in after] == [
        {key: value for key, value in row.items() if key != 'filename'} for row in before]
    assert {path: replay_runtime.sha256(path) for path in source_hashes} == source_hashes
    # Simulate collection of the original immutable files after the copy.
    for path in source_hashes:
        Path(path).unlink()
    with pytest.MonkeyPatch.context() as patch:
        calls = replay_storage.guard_reads(patch, campaign)
        assert policy_documents.read(after[0]['filename'], after[0]['digest'],
            offset=after[0]['offset'], size=after[0]['bytes']) == {'setting': 1}
        assert recommendation_outputs.read(after[-1]['filename'], after[-1]['digest'], after[-1]['bytes'],
            surface='radio', chunk_key='framing') == {'tracks': ['a', 'b']}
        with pytest.raises(ValueError, match='unarchived payload read'):
            policy_documents.read(before[0]['filename'], before[0]['digest'])
        # Another valid archive under the research root is still a different
        # campaign; it cannot supply a missing file for this run.
        sibling = campaign.with_name('paired-slices-other') / 'policies'
        other_file, checksum, size, offset = next(iter(policy_documents.publish_many(sibling, [{'setting': 3}])))
        with pytest.raises(ValueError, match='unarchived payload read'):
            policy_documents.read(other_file, checksum, offset=offset, size=size)
        assert calls == {'policy_reads': 1, 'prepared_output_reads': 1, 'outside_boundary_reads': 2}


def test_missing_consumer_payload_cannot_partially_relocate_sql(tmp_path, monkeypatch):
    database, campaign = private_payload_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(database) as conn:
        rows = replay_storage.owned_rows(conn)
    original_database = replay_runtime.sha256(database)
    Path(rows[-1]['filename']).unlink()
    with pytest.raises(FileNotFoundError):
        replay_storage.freeze(database, campaign)
    assert replay_runtime.sha256(database) == original_database
    assert not (campaign / 'payload-manifest.json').exists()


@pytest.mark.parametrize('change', ['content', 'extra_file'])
def test_consumer_resume_rejects_changed_payload_archive(tmp_path, monkeypatch, change):
    database, campaign = private_payload_fixture(tmp_path, monkeypatch)
    report = replay_storage.freeze(database, campaign)
    expected = replay_runtime.sha256(campaign / 'payload-manifest.json')
    replay_storage.verify(campaign, expected)
    if change == 'content':
        (campaign / next(iter(report['files']))).write_bytes(b'changed')
    else:
        (campaign / 'payloads/untracked.json').write_text('{}')
    with pytest.raises(ValueError, match='Consumer payload'):
        replay_storage.verify(campaign, expected)
