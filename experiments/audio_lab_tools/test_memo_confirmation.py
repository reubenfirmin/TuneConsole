"""Focused missingness and component-resampling checks for #710."""
import numpy as np
from experiments.audio_lab_tools.memo_confirmation import targets, holdout_pairs


def test_blank_trajectory_is_missing(tmp_path):
    p = tmp_path/'va.csv'
    p.write_text('song_id,'+','.join(f'sample_{s*1000}ms' for s in range(15,75))+'\n1,'+','.join(['0']*59+[''])+'\n2,'+','.join(['1']*60)+'\n')
    result = targets({'valence':p},['1','2','3'])
    assert result['1']['valence']['value'] is None
    assert result['1']['valence']['missing_seconds']==[74]
    assert result['2']['valence']['value']==1
    assert result['3']=={}


def test_component_paired_holdout_identity():
    y = np.array([0.,.1,.6,.7,1.,.9])
    result = holdout_pairs(y,{'a':y,'b':y},np.array([0,0,1,1,2,2]),[('a','b')],100)
    for cell in result['a__vs__b'].values():
        assert cell['difference']==0
        assert cell['conditional_95_percent_interval']==[0,0]


def test_component_paired_detects_reversed_ranking():
    y = np.linspace(0,1,20)
    result = holdout_pairs(y,{'a':y,'b':1-y},np.arange(20),[('a','b')],100)
    assert result['a__vs__b']['ndcg']['conditional_95_percent_interval'][0]>.5
