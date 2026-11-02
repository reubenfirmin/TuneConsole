"""Actual SiTunes split, label, preprocessing and exported inference checks."""
from collections import Counter

import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.situnes_study import DEFAULT, AXES, FAMILIES, cohort, matrix, schema
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import verify


def test_actual_cohort_and_all_group_boundaries():
    assert verify(DEFAULT,required_stages=['cohort','predictions','evaluation'],
                  optional_stages=['postrun-source'])['receipts'] in (3,4)
    rows, stages, _ = cohort(DEFAULT)
    assert [stages[str(s)]['rows'] for s in (1,2,3)] == [600,897,509]
    assert len(rows) == 1406 and len({r['id'] for r in rows}) == 1406
    assert len({r['user'] for r in rows}) == 30
    assert len(load(DEFAULT / 'audio-audit.json')['candidates']) == 12
    assert load(DEFAULT / 'audio-audit.json')['paired_rows'] == 39
    plans = load(DEFAULT / 'partitions.json')
    for p in plans:
        train, test = p['train'], p['test']
        assert not set(train)&set(test)
        assert set(train)|set(test) == set(range(1406))
        key = 'track' if p['evaluation']=='track' else 'user'
        if p['evaluation'] != 'stage2-to-stage3':
            assert not {rows[i][key] for i in train}&{rows[i][key] for i in test}
        else:
            assert {rows[i]['stage'] for i in train} == {'2'}
            assert {rows[i]['stage'] for i in test} == {'3'}
        for fold in sorted(set(p['inner'])):
            inner_train = {rows[i][key] for i,f in zip(train,p['inner'],strict=True) if f != fold}
            inner_valid = {rows[i][key] for i,f in zip(train,p['inner'],strict=True) if f == fold}
            assert not inner_train&inner_valid
    for evaluation, count in [('participant',30),('track',5),('stage2-to-stage3',1)]:
        selected = [p for p in plans if p['evaluation']==evaluation]
        assert len(selected)==count
        seen = Counter(i for p in selected for i in p['test'])
        expected = set(range(1406)) if count>1 else {i for i,r in enumerate(rows) if r['stage']=='3'}
        assert set(seen)==expected and set(seen.values())=={1}


def test_actual_training_vocabularies_and_all_exported_predictions():
    rows = load(DEFAULT / 'cohort.json')['rows']
    pred = arrays(DEFAULT / 'predictions.npz')
    exports = load(DEFAULT / 'models.json')
    with threadpool_limits(limits=1):
        for p in exports:
            training = [rows[i] for i in p['train']]
            testing = [rows[i] for i in p['test']]
            train_y = np.array([r['post'] for r in training])
            for family, model in p['models'].items():
                assert model['schema'] == schema(training,family)
                for inner in model['inner_schemas']:
                    fit_rows = [r for r,f in zip(training,p['inner'],strict=True) if f != inner['fold']]
                    assert inner['schema'] == schema(fit_rows,family)
                x = matrix(testing,model['schema'])
                train_x = matrix(training,model['schema'])
                medians = np.array([np.median(c[np.isfinite(c)]) if np.isfinite(c).any() else 0 for c in train_x.T])
                clean_train = np.where(np.isfinite(train_x),train_x,medians)
                scale = clean_train.std(axis=0)
                scale[scale < 1e-9] = 1
                columns = []
                for head in model['heads']:
                    if head['kind']=='constant':
                        v = np.full((len(x),1),head['value'][0])
                    else:
                        # Exported-form inference, independent of study replay/native_predict.
                        np.testing.assert_array_equal(head['medians'],medians)
                        np.testing.assert_array_equal(head['mean'],clean_train.mean(axis=0))
                        np.testing.assert_array_equal(head['scale'],scale)
                        clean = np.where(np.isfinite(x),x,np.array(head['medians']))
                        z = (clean-np.array(head['mean']))/np.array(head['scale'])
                        v = np.clip(z@np.array(head['weights'])+np.array(head['intercept']),0,1)
                    columns.append(v.ravel())
                result = np.column_stack(columns)*2-1
                np.testing.assert_array_equal(result,pred[p['evaluation']+'__'+family][p['test']])
                selected = np.array(model['inner_mse_normalized']).argmin(axis=0).tolist()
                assert selected == model['selected']
            # Averaging after affine normalization changes accumulation roundoff.
            np.testing.assert_allclose(pred[p['evaluation']+'__mean'][p['test']],np.tile(train_y.mean(axis=0),(len(testing),1)),rtol=0,atol=1e-14)
            np.testing.assert_array_equal(pred[p['evaluation']+'__unchanged'][p['test']],[r['pre'] for r in testing])
            if p['evaluation']=='participant':
                np.testing.assert_array_equal(pred['participant__participant-mean'][p['test']],pred['participant__mean'][p['test']])
    assert set(exports[0]['models']) == set(FAMILIES)


def test_actual_metrics_and_absence_of_post_listening_features():
    rows = load(DEFAULT / 'cohort.json')['rows']
    pred = arrays(DEFAULT / 'predictions.npz')
    report = load(DEFAULT / 'report.json')
    y = np.array([r['post'] for r in rows])
    for evaluation, results in report['evaluations'].items():
        for name, metrics in results['metrics'].items():
            value = pred[evaluation+'__'+name]
            keep = np.isfinite(value).all(axis=1)
            np.testing.assert_array_equal(np.mean((value[keep]-y[keep])**2,axis=0),metrics['mse'])
            assert results['rows'] == int(keep.sum())
        for result in results['paired'].values():
            assert set(result['usable_draws'].values()) == {1000}
    full = schema(rows,FAMILIES[-1])
    assert set(full['categories']) == {'stage','policy','time','weather','user','genre'}
    assert set(full['numeric'][:5]) == {'pre-valence','pre-arousal','pressure','temperature','humidity'}
    assert all(k not in full['numeric'] for k in ('post','rating','preference','duration','timestamp','item'))
    changed = [{**r,'post':[999,-999],'rating':999,'preference':999,'duration':999,'timestamp':999} for r in rows[:4]]
    np.testing.assert_array_equal(matrix(changed,full),matrix(rows[:4],full))
    # Unseen levels must not create a column learned from evaluation records.
    unknown = {**rows[0], **{k:'never-in-training' for k in full['categories']}}
    assert matrix([unknown],full)[0,len(full['numeric']):].sum() == 0
    assert report['axes'] == list(AXES)
