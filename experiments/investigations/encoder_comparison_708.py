"""Independent identity, interval, exclusion and numerical evidence checks."""
from collections import defaultdict
import numpy as np

from experiments.audio_lab_tools.studies.encoder_experiment import DEFAULT
from experiments.audio_lab_tools.studies import encoder_models as models
from experiments.audio_lab_tools.focus.focus_experiment import arrays, load
from experiments.audio_lab_tools.support.quality_round import verify, validate
from experiments.audio_lab_tools.support.external_experiment import save


def test_feature_reuse_provenance():
    from pathlib import Path
    from experiments.audio_lab_tools.support.external_experiment import digest
    plan=validate(DEFAULT)
    assert 'audio_lab_tools/memo_confirmation.py' in plan['code']
    assert 'audio_lab_tools/encoder_experiment.py' in plan['code']
    assert 'investigations/encoder_comparison_708.py' in plan['code']
    origin=Path(plan['protocol']['feature_source']['directory'])
    assert origin.name=='round-1'
    verify(origin)
    assert not (origin/'predictions.receipt.json').exists()
    assert not (origin/'report.json').exists()
    receipt=load(origin/'extraction.receipt.json')
    for name,checksum in receipt['outputs'].items():
        assert digest(DEFAULT/name)==checksum
    audit=load(DEFAULT/'feature-reuse.json')
    assert audit['new_encoder_calls']==0
    assert audit['source_plan_sha256']==digest(origin/'plan.json')
    assert load(DEFAULT/'extraction.json')['resources']==load(origin/'extraction.json')['resources']


def test_frozen_selection():
    plan=validate(DEFAULT)
    selected=load(DEFAULT/'inputs/selection.json')['rows']
    assert len(selected)==288
    assert sum(len(r['chunk_starts']) for r in selected)==576
    assert len([p for p in plan['inputs'] if p.startswith('inputs/identities/')])==16
    from experiments.audio_lab_tools.support.quality_round import reserve_memo
    reservation=reserve_memo()
    old=set(reservation['old_pilot_names'])
    from pathlib import Path
    for source in models.SOURCES:
        rows=[r for r in selected if r['source']==source]
        assert len(rows)==32
        counts=defaultdict(int)
        for row in rows:
            counts[row['component']]+=1
            if source==models.MEMO:
                assert Path(row['audio']).name in old
                assert row['chunk_starts']==[15,25,35,45,55,65]
        assert max(counts.values())<=2
    group_folds=defaultdict(set)
    for row in selected:group_folds[row['component']].add(row['outer'])
    assert all(len(v)==1 for v in group_folds.values())


def test_musicnn_interval_parity():
    data=arrays(DEFAULT/'features.npz')
    rows=load(DEFAULT/'extraction.json')['rows']
    audit={}
    for source in models.SOURCES:
        original=arrays(DEFAULT/f'inputs/{source}.npz')
        errors=[]
        for i,row in enumerate(rows):
            if row['source']!=source:continue
            j=row['row_index']
            if 'offsets' in original:
                first,last=original['offsets'][j:j+2]
                expected=original['embedding'][first:last]
            else:expected=original['embedding'][j:j+1]
            actual=data['musicnn'][i,:data['counts'][i]]
            assert actual.shape==expected.shape
            errors.append(float(np.max(np.abs(actual-expected))))
        audit[source]={'recordings':len(errors),'maximum_error':max(errors)}
        assert max(errors)<.0005
    save(DEFAULT/'musicnn-parity.json',audit)
    from experiments.audio_lab_tools.support.quality_round import seal
    seal(DEFAULT,'musicnn-parity',[DEFAULT/'musicnn-parity.json'])
    print('Exact interval old-reference parity:',audit)


def test_interval_pcm_and_mert_row_order():
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import time
    from experiments.audio_lab_tools.support.external_experiment import digest, pcm
    from experiments.audio_lab_tools.support.quality_round import seal
    rows=load(DEFAULT/'extraction.json')['rows']
    features=arrays(DEFAULT/'features.npz')
    cached={}
    for path in (DEFAULT/'mert-chunks').glob('*.json'):
        record=load(path)
        key=(record['input']['audio_sha256'],record['input']['start'])
        assert key not in cached
        cached[key]=record
    started=time.monotonic()
    audit=[]
    with TemporaryDirectory(prefix='pcm-audit-',dir=DEFAULT) as temporary:
        signal=Path(temporary)/'interval.f32'
        for i,row in enumerate(rows):
            assert features['counts'][i]==len(row['chunk_starts'])
            for j,start in enumerate(row['chunk_starts']):
                record=cached[(row['audio_sha256'],start)]
                assert record['input']['sample_rate']==24000
                assert record['input']['seconds']==10
                pcm(row['audio'],start,24000,1,signal)
                assert signal.stat().st_size==240000*4
                high_rate=digest(signal)
                assert high_rate==record['result']['pcm_sha256']
                np.testing.assert_array_equal(features['mert'][i,j],record['result']['embedding'])
                pcm(row['audio'],start,16000,1,signal)
                assert signal.stat().st_size==160000*4
                audit.append({'source':row['source'],'id':row['id'],'start':start,
                    'pcm_24000_sha256':high_rate,'pcm_16000_sha256':digest(signal)})
    assert len(audit)==576
    save(DEFAULT/'pcm-audit.json',{'intervals':audit,'extra_decode_audit_seconds':time.monotonic()-started,
        'scope':'Fresh PCM verification; no encoder calls. MusiCNN feature fidelity is checked separately against the original vectors.'})
    seal(DEFAULT,'pcm-audit',[DEFAULT/'pcm-audit.json'])


def test_all_selection_exclusions():
    rows=load(DEFAULT/'extraction.json')['rows']
    traces=load(DEFAULT/'fit-trace.json')['contexts']
    assert len(traces)==26
    for representation in ('musicnn','mert'):
        for key,context in traces.items():
            if not key.startswith(representation+'__'):continue
            protected=set(context['protected_components'])
            if '__outer-' in key:
                fold=int(key.rsplit('-',1)[1])
                assert protected=={r['component'] for r in rows if r['outer']==fold}
            else:
                omitted=context['omitted_sources']
                assert omitted
                assert not (set(omitted)&set(context['sources']))
                assert protected=={r['component'] for r in rows if r['source'] in omitted}
                if models.NAT in omitted:assert set(omitted)=={models.NAT,models.GEN}
            for source,trace in context['sources'].items():
                assert not (protected&set(trace['training_groups']))
                lookup={r['id']:r for r in rows if r['source']==source}
                assert trace['training_groups']==[lookup[k]['component'] for k in trace['training_ids']]
                groups=defaultdict(set)
                for g,fold in zip(trace['training_groups'],trace['inner_folds'],strict=True):groups[g].add(fold)
                assert all(len(v)==1 for v in groups.values())
                assert len(set(trace['inner_folds']))==3
                assert trace['specs']==[{'family':'mean'},*models.protocol.SPECS]


def test_completed_result_and_resources():
    result=verify(DEFAULT)
    assert result['receipts']>=3
    report=load(DEFAULT/'report.json')
    extraction=load(DEFAULT/'extraction.json')
    assert report['scope']['new_memo_confirmation_records_used']==0
    assert report['scope']['retained']==len(extraction['rows'])
    assert len(report['decision'])==6
    assert len(report['native'])==24
    for cell in report['tables'].values():
        assert cell['rows']==32
        assert cell['components']>=12
        assert set(cell['metrics'])=={'musicnn','mert','current_exposed'}
        assert cell['paired']['mert__vs__musicnn']['ndcg']['draws']==1000
    resources=report['resources']
    assert not resources['resource_failures']
    assert resources['extraction']['mert_replay_max_error']<=1e-6
    assert resources['extraction']['mert_runtime']['threads']==1
    assert not [p for p in DEFAULT.glob('*pcm-*') if p.is_dir()]
    prediction=arrays(DEFAULT/'predictions.npz')
    for vibe in models.VIBES:
        for representation in ('musicnn','mert'):
            values=prediction[f'{representation}__oof__{vibe}']
            assert np.isfinite(values).all()
            assert values.min()>=0 and values.max()<=1
            assert resources['fit']['head_replay'][representation]['maximum_error']==0
