"""Identical nested ridge selection and fixed specialist mappings for #708."""
import numpy as np

from . import individual_protocol as protocol
from ..training.external_training import folds
from ..focus.focus_experiment import constant
from .mapped_vibe_inference import replay
from .mapped_vibe_rules import apply, registry
from ..uplift.uplift_consensus import native_average

CAL,JAM,EMO,SOUND = 'cal500-raw-activity','jamendo-human-4shards','emotify-artist','soundtracks-all'
DEAM,PMEMO,NAT,GEN,MEMO = 'deam','pmemo','manymusic-natural','manymusic-generated','memo2496-pilot'
SOURCES = (DEAM,PMEMO,SOUND,JAM,EMO,NAT,GEN,MEMO,CAL)
VA = (DEAM,PMEMO,NAT,GEN,MEMO)
NATIVE = {s:['valence','arousal'] for s in VA}
NATIVE.update({SOUND:['valence','energy','tension','happy','tender','anger'],
    EMO:['calmness','tension','joyful_activation','power'],JAM:['happy','aggressive'],
    CAL:['Usage-Reading','Usage-Studying']})
VIBES = ('cruise','relax','uplift','energize','focus','release')
SEED = 7081101


def choose(chunks,counts,y,groups):
    """Training-fold normalization; inner error uses actual native chunk averaging."""
    split = folds(groups,count=3,seed=SEED)
    protocol.group_guard(groups,split)
    x = np.array([row[:n].mean(0) for row,n in zip(chunks,counts,strict=True)])
    predictions = np.empty((len(y),6,y.shape[1]))
    for fold in sorted(set(split)):
        train,valid = split!=fold,split==fold
        fitted = [constant(y[train],x.shape[1]),*protocol.learner.fit_models(x[train],y[train],protocol.SPECS)]
        for i,model in enumerate(fitted):
            values = replay(protocol.learner.serial(model),chunks[valid].reshape(-1,x.shape[1])).reshape(valid.sum(),chunks.shape[1],-1)
            predictions[valid,i] = np.array([v[:n].mean(0) for v,n in zip(values,counts[valid],strict=True)])
    loss = np.mean((predictions-y[:,None,:])**2,axis=0)
    selected = loss.argmin(0)
    full = [constant(y,x.shape[1]),*protocol.learner.fit_models(x,y,protocol.SPECS)]
    heads = []
    for column,index in enumerate(selected):
        model = full[index]
        head = {**model,'value':model['value'][column:column+1]} if model['kind']=='constant' else protocol.learner.column_model(model,column)
        heads.append(protocol.learner.serial(head))
    return heads,{'selected':selected.tolist(),'inner_mse':loss.tolist(),'inner_folds':split.tolist(),
        'training_groups':groups.tolist(),'specs':[{'family':'mean'},*protocol.SPECS]}


def expressions(source):
    native = source if source not in (NAT,GEN,MEMO) else PMEMO
    return {('focus' if vibe=='concentrate' else vibe):r['expression'] for vibe,r in registry()[native].items()
            if vibe in ('relax','cruise','uplift','energize','release','concentrate')}


def expert_score(vibe,source,values):
    names = NATIVE[source]
    v = {n:values[:,i] for i,n in enumerate(names)}
    if vibe=='focus':return (v['Usage-Reading']+v['Usage-Studying'])/2
    if vibe=='release':return v['aggressive']
    if vibe=='cruise':return (1-2*np.abs(v['arousal']-.5)+v['valence'])/2
    if vibe=='uplift':
        if source==SOUND:return (v['valence']+v['happy'])/2
        if source==EMO:return v['joyful_activation']
        if source==JAM:return v['happy']
        return v['valence']
    if vibe=='energize':return v[{SOUND:'energy',EMO:'power'}.get(source,'arousal')]
    if vibe=='relax':
        if source==EMO:return (v['calmness']+1-v['tension'])/2
        if source==SOUND:return (2-v['energy']-v['tension']+v['tender'])/3
        return (1-v['arousal']+v['valence'])/2
    raise ValueError(vibe)


def mixture(vibe,native,weights):
    available = {s:w for s,w in weights[vibe].items() if s in native}
    if not available:return None
    total = sum(available.values())
    return sum(expert_score(vibe,s,native[s])*w/total for s,w in available.items())


def predict(models,chunks,counts):
    return {s:native_average(heads,chunks,counts,independent=True) for s,heads in models.items()}


def family(source):
    return (NAT,GEN) if source in (NAT,GEN) else (source,)
