"""Fixed transformations of external annotations and native predictions alike.

These rules are declared before the new fits. They do not estimate listening
benefit from local labels. Missing support is an absent mapping, never zero.
"""
import numpy as np

VIBES = ('relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release')
VA = ('pmemo', 'deam', 'vgmidi-original', 'merp-static',
      'amg1608-matched', 'ntwicm-matched')
DATASETS = ('cal500-raw-activity', 'emotify-artist', *VA, 'soundtracks-all',
            'jamendo-human-4shards', 'coimbra-mirex-like', 'merge-quadrants',
            'emma-preview-matched')


def mean(*args):
    return {'op': 'mean', 'args': list(args)}


def invert(label):
    return {'op': 'invert', 'args': [label]}


def middle(label):
    return {'op': 'middle', 'args': [label]}


def add(*args):
    return {'op': 'sum', 'args': list(args)}


def registry():
    rules = {}

    def put(dataset, expressions, semantics):
        rules[dataset] = {vibe: {'expression': expression, 'semantics': semantics,
                               'scale': 'original native annotation normalized to [0,1]',
                               'missing': 'unsupported; raise on absent required column'}
                          for vibe, expression in expressions.items()}

    put('cal500-raw-activity', {
        'concentrate': mean('Usage-Reading', 'Usage-Studying'),
        'concentrate_work_ablation': mean('Usage-Reading', 'Usage-Studying', 'Usage-At_work'),
        'relax': mean('Usage-Going_to_sleep', 'Usage-Sleeping'),
        'energize': 'Usage-Exercising',
    }, 'human whole-song activity vote fractions on our 10-second analysis; sleep is a relaxation proxy')
    for dataset, calm, joy in (('emotify-artist', 'calmness', 'joyful_activation'),
                               ('emma-preview-matched', 'peacefulness', 'joy')):
        put(dataset, {
            'relax': mean(calm, invert('tension')),
            'cruise': mean(middle('power'), calm, invert('tension')),
            'energize': mean('power', joy),
            'uplift': joy,
            'release': 'power',
        }, 'mapped human emotion ratings; power does not require fast tempo or negative valence')
    for dataset in VA:
        put(dataset, {
            'relax': mean(invert('arousal'), 'valence'),
            'cruise': mean(middle('arousal'), 'valence'),
            'energize': 'arousal',
            'uplift': 'valence',
        }, 'mapped valence/arousal ratings; no direct Focus or aggression support')
    put('soundtracks-all', {
        'relax': mean(invert('energy'), invert('tension'), 'tender'),
        'cruise': mean(middle('energy'), invert('anger'), invert('tension')),
        'energize': 'energy',
        'uplift': mean('happy', 'valence'),
        'release': 'anger',
    }, 'mapped human excerpt emotion ratings; anger supplies force, independently of energy')
    put('jamendo-human-4shards', {
        'relax': 'relaxed', 'energize': 'party', 'uplift': 'happy', 'release': 'aggressive',
    }, 'whole-song human vote fractions; party is an activation proxy')
    put('coimbra-mirex-like', {
        'energize': 'rousing', 'uplift': 'cheerful', 'release': 'aggressive',
    }, 'tag-derived class marginals; weaker evidence than direct listener ratings')
    put('merge-quadrants', {
        'relax': 'low_positive',
        'energize': add('high_positive', 'high_negative'),
        'uplift': add('high_positive', 'low_positive'),
    }, 'validated tag-derived quadrants; no observed moderate-activation or aggression label')
    return rules


def labels(expression):
    if isinstance(expression, str):
        return {expression}
    return set().union(*(labels(arg) for arg in expression['args']))


def apply(expression, values, names):
    """Support row, candidate, and column dimensions without changing fractions."""
    values = np.asarray(values, dtype=float)
    if isinstance(expression, str):
        if expression not in names:
            raise ValueError('Unsupported missing annotation: ' + expression)
        return values[..., names.index(expression)]
    args = [apply(arg, values, names) for arg in expression['args']]
    operation = expression['op']
    if operation == 'mean':
        return np.mean(args, axis=0)
    if operation == 'invert':
        return 1 - args[0]
    if operation == 'middle':
        return 1 - 2 * np.abs(args[0] - .5)
    if operation == 'sum':
        return np.clip(np.sum(args, axis=0), 0, 1)
    raise ValueError('Unknown annotation operation: ' + operation)


# Sources are fixed by annotation meaning/provenance, not by comparing metrics
# across different corpora. Each source's model is selected on its own training
# folds. Other source experts remain exported alternatives.
ANCHORS = {'relax': 'emotify-artist', 'concentrate': 'cal500-raw-activity',
           'cruise': 'emotify-artist', 'energize': 'pmemo', 'uplift': 'pmemo',
           'release': 'jamendo-human-4shards'}
TRANSFER = [(a, b) for a in VA for b in VA if a != b]
TRANSFER += [('emotify-artist', 'emma-preview-matched'),
             ('emma-preview-matched', 'emotify-artist')]
