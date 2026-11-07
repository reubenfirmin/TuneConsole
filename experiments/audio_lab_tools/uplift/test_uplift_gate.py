"""Independent invariants for the constrained mixer and grouped fitting."""
import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools.uplift import uplift_gate as m, uplift_consensus as n


def test_zero_gate_exactly_recovers_fixed_consensus():
    rng = np.random.default_rng(712)
    for names in (list(n.EXPERTS), [s for s in n.EXPERTS if s != n.SOUND]):
        matrix = rng.uniform(size=(20, len(names)))
        np.testing.assert_allclose(m.mix(names, matrix, rng.uniform(size=20), (0, 0)),
                                   matrix @ n.fixed_weights(names), atol=1e-15)


def test_gate_convexity_and_no_dataset_feature():
    a = np.linspace(0, 1, 100)
    for parameter in m.GRID:
        weights = m.gate(a, parameter)
        assert np.all((weights > 0) & (weights < 1))
        valence, emotion = a, 1 - a
        out = weights * valence + (1 - weights) * emotion
        assert np.all(out >= np.minimum(valence, emotion))
        assert np.all(out <= np.maximum(valence, emotion))


def test_complete_cross_source_assignment():
    data = {source: {'groups': np.arange(30)} for source in n.SOURCES}
    split = n.split(data)
    for source in data:
        np.testing.assert_array_equal(split[source], split[n.DEAM])
        assert len(set(split[source])) == 3


def test_every_gate_validation_component_is_absent_from_every_head():
    rng = np.random.default_rng(712)
    data = {}
    for source in n.SOURCES:
        chunks = rng.normal(size=(36, 2, 4))
        y = rng.uniform(size=(36, len(n.NATIVE[source])))
        data[source] = {'x': chunks.mean(1), 'chunks': chunks, 'counts': np.full(36, 2),
            'y': y, 'reference': y.mean(1), 'arousal': rng.uniform(size=36),
            'groups': np.repeat(np.arange(18), 2), 'ids': np.array([source + str(i) for i in range(36)])}
    with threadpool_limits(limits=1):
        model, trace = m.fit(data)
        expected = m.predict(model, data[n.MEMO]['chunks'], data[n.MEMO]['counts'])
        replay = m.predict(model, data[n.MEMO]['chunks'], data[n.MEMO]['counts'], True)
    for name in expected:
        np.testing.assert_allclose(expected[name], replay[name], atol=1e-12)
    for stage in trace['crossfit'].values():
        valid = {g for value in stage['validation'].values() for g in value['groups']}
        for head in [*stage['native'].values(), *stage['arousal'].values()]:
            assert not valid & set(head['training_groups'])
            assigned = {}
            for group, fold in zip(head['training_groups'], head['inner_folds'], strict=True):
                assert assigned.setdefault(group, fold) == fold
