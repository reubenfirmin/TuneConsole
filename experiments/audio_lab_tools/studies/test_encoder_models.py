"""Checks for fixed semantics and actual chunk aggregation in #708."""
import numpy as np
from experiments.audio_lab_tools.studies import encoder_models as m


def test_fixed_power_and_aggression_semantics():
    values=np.array([[.2,.4,.9,.3]])
    assert m.expert_score('energize',m.EMO,values)[0]==.3
    assert m.expert_score('uplift',m.EMO,values)[0]==.9
    assert m.expert_score('release',m.JAM,np.array([[.8,.1]]))[0]==.1
    assert m.expert_score('cruise',m.DEAM,np.array([[1.,.5]]))[0]==1
    assert m.expert_score('cruise',m.DEAM,np.array([[0.,1.]]))[0]==0


def test_omission_preserves_fixed_ratios_and_support():
    native={m.DEAM:np.array([[.8,.1]]),m.PMEMO:np.array([[.4,.9]])}
    weights={'uplift':{m.DEAM:.125,m.PMEMO:.125,m.EMO:.125},'focus':{m.CAL:1}}
    assert np.allclose(m.mixture('uplift',native,weights),[.6])
    assert m.mixture('focus',native,weights) is None
    assert m.family(m.NAT)==m.family(m.GEN)==(m.NAT,m.GEN)


def test_padding_cannot_enter_selection_or_prediction():
    rng=np.random.default_rng(708)
    x=rng.normal(size=(18,2,4))
    counts=np.ones(18,dtype=int)
    y=np.clip(.5+.2*x[:,0,:1],0,1)
    groups=np.arange(18)
    first,trace=m.choose(x,counts,y,groups)
    x[:,1]=1e9
    second,other=m.choose(x,counts,y,groups)
    assert first==second
    assert trace==other
    assert set(trace['training_groups'])==set(groups)
    assert len(set(trace['inner_folds']))==3


def test_undefined_primary_ranking_cannot_advance():
    from experiments.audio_lab_tools.studies.encoder_experiment import decision
    # A constant-label destination has no ranking evidence even when another
    # destination has a large, precise gain. It must stay in the decision.
    def cell(value,interval):
        return {'vibe':'relax','primary':True,'rows':32,'components':32,
            'paired':{'mert__vs__musicnn':{
                'ndcg':{'difference':value,'conditional_95_percent_interval':interval},
                'top_lift':{'difference':value},'mse':{'difference':0.}}}}
    result=decision('relax',{'varying':cell(.1,[.05,.15]),'constant':cell(None,None)})
    assert result['recommendation']=='retain'
    assert result['primary_cases']==['varying','constant']
    assert result['mean_primary_difference']['ndcg'] is None
    assert any('constant' in reason for reason in result['reasons'])
