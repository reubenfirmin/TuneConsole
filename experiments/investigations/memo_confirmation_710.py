"""Memo confirmation metadata and saved-evidence investigations."""
import urllib.error
import urllib.request

from experiments.audio_lab_tools.external_experiment import DATA
from experiments.audio_lab_tools.focus_experiment import load


def test_acoustic_worker_parity(tmp_path):
    import json
    from experiments.audio_lab_tools.external_experiment import LAB, pcm
    from experiments.audio_lab_tools.memo_confirmation_audio import measurements
    from experiments.audio_lab_tools.quality_reference_audio import response
    row = load(LAB / 'local/external-20261028/deam.features.json')['rows'][0]
    old = load(row['feature_file'])
    print('Stored schema:', list(old))
    destination = tmp_path / 'measurement.f32'
    pcm(row['audio'], row['sample_start'], 44100, 2, destination)
    with measurements(tmp_path) as worker:
        worker.stdin.write(json.dumps({'id': row['id'], 'pcm': str(destination), 'channels': 2}) + '\n')
        worker.stdin.flush()
        actual = response(worker, 'RESULT ')['attributes']['values']
    expected = old['audio_attributes']['values']
    assert actual.keys() == expected.keys()
    error = max(abs(actual[k]-expected[k]) for k in actual if actual[k] is not None and expected[k] is not None)
    assert all((actual[k] is None) == (expected[k] is None) for k in actual)
    print('Exact acoustic parity max error:', error)
    assert error < 1e-12


def test_range_endpoint_diagnostic():
    metadata = load(DATA / 'discovery/memo2496-figshare.json')
    item = next(r for r in metadata['files'] if r['name'] == 'MusicRawData.zip')
    request = urllib.request.Request(item['download_url'], headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print({'status': response.status, 'range': response.headers.get('Content-Range'),
                   'one_byte': response.read(1).hex()})
    except urllib.error.HTTPError as error:
        print({'status': error.code, 'server': error.headers.get('Server'),
               'body': error.read(300).decode(errors='replace')})
        raise


def test_frozen_cohort_and_exclusions():
    from experiments.audio_lab_tools.memo_confirmation import DEFAULT
    from experiments.audio_lab_tools.quality_round import validate
    plan = validate(DEFAULT)
    selection = load(DEFAULT/'inputs/selection.json')['rows']
    reservation = load(DEFAULT/'inputs/reservation.json')
    assert len(selection)==128
    assert {r['file_name'] for r in selection}=={r['name'] for r in reservation['selected']}
    assert not ({r['file_name'] for r in selection} & set(reservation['old_pilot_names']))
    assert sum(r['member']['bytes'] for r in selection)<=1024**3
    assert plan['protocol']['interval']==[15,75]
    assert plan['protocol']['chunks']==[15,25,35,45,55,65]
    assert all(not r['artist'] for r in selection)


def test_old_pilot_both_preparations(tmp_path):
    import shutil
    from pathlib import Path
    import numpy as np
    from experiments.audio_lab_tools.memo_confirmation import PREVIOUS
    from experiments.audio_lab_tools.memo_confirmation_audio import extract
    row = load(PREVIOUS/'inputs/memo2496-pilot.json')['rows'][0]
    path = tmp_path/'audio'/Path(row['audio']).name
    path.parent.mkdir()
    shutil.copyfile(row['audio'],path)
    names = load(PREVIOUS/'plan.json')['attribute_names']
    rows, errors, resources, caches = extract(tmp_path,[{**row,'audio':str(path),'file_name':path.name}],names)
    assert not errors
    assert len(rows)==1 and len(caches)==12
    assert resources['new_chunks']==12
    for chunk in rows[0]['chunks']:
        for prep in ('historical','browser'):
            assert np.asarray(chunk[prep]['embedding']).shape==(200,)
            assert sorted(chunk[prep]['attributes'])==names
    print('Both-preparation old-pilot acceptance:', resources)


def test_completed_confirmation_evidence():
    import numpy as np
    from experiments.audio_lab_tools.memo_confirmation import DEFAULT
    from experiments.audio_lab_tools.quality_round import verify
    from experiments.audio_lab_tools.focus_experiment import arrays
    result = verify(DEFAULT)
    assert result['receipts']>=4
    acquisition = load(DEFAULT/'acquisition.json')
    assert acquisition['requested_bytes']<=1024**3
    assert acquisition['audio_bytes']<=1024**3
    features = load(DEFAULT/'features.json')
    prediction = arrays(DEFAULT/'predictions.npz')
    trace = load(DEFAULT/'prediction-trace.json')
    report = load(DEFAULT/'report.json')
    assert trace['fit_count']==0 and trace['new_targets_opened'] is False
    assert report['outcome_order']['predictions_sealed']<=report['outcome_order']['targets_opened']
    assert not acquisition['known_component_exclusions']
    for row in features['rows']:
        assert [c['start'] for c in row['chunks']]==[15,25,35,45,55,65]
        assert row['covered_interval']==[15,75]
        for c in row['chunks']:
            for prep in ('historical','browser'):
                assert np.isfinite(c[prep]['embedding']).all()
                assert len(c[prep]['embedding'])==200
    for key, values in prediction.items():
        assert len(values)==len(features['rows'])
        assert np.isfinite(values).all()
        assert values.min()>=0 and values.max()<=1
    assert len(report['tables'])==8
    assert not list(DEFAULT.glob('worker-*/pcm-*'))
    assert not list(DEFAULT.glob('ranges-*'))


def test_rejected_alignment_audit():
    from experiments.audio_lab_tools.memo_confirmation import DEFAULT
    from experiments.audio_lab_tools.external_experiment import probe, digest, save
    from experiments.audio_lab_tools.quality_round import seal
    rejected = {r['id'] for r in load(DEFAULT/'features.json')['rejected']}
    rows = [r for r in load(DEFAULT/'acquisition.json')['rows'] if r['id'] in rejected]
    audit = []
    for row in rows:
        duration, channels = probe(row['audio'])
        assert digest(row['audio'])==row['audio_sha256']
        assert channels in (1,2)
        assert duration<75
        audit.append({'id':row['id'],'duration_seconds':duration,'channels':channels,'audio_sha256_verified':True,
            'reason':'Source ends before the required [15,75) interval completes.'})
    save(DEFAULT/'alignment-audit.json',{'rejected':audit})
    seal(DEFAULT,'alignment-audit',[DEFAULT/'alignment-audit.json'])
    print('Independent short-source audit:',audit)
