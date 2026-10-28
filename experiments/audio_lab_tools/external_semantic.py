"""Fixed semantic mappings from external native labels; no gold-label fitting."""
import json
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

from .external_binary_calibration import metrics
from .external_experiment import OUT, digest, save, stamp
from .external_learners import serializable
from .external_training import predict
from .external_transfer import SNAPSHOT, VIBES, external_head_features

# These equal-weight rules are fixed before running this trial. They preserve
# the existing six meanings and make their imperfect semantic proxies explicit.
GEMS_RULES = {
    'relax': [('calm', '+'), ('power', '-'), ('tension', '-')],
    'concentrate': [('calm', '+'), ('joy', '-'), ('power', '-'), ('tension', '-')],
    'cruise': [('power', 'mid'), ('joy', '+'), ('tension', '-')],
    'energize': [('power', '+'), ('joy', '+')],
    'uplift': [('joy', '+'), ('sadness', '-')],
    'release': [('power', '+'), ('tension', '+'), ('calm', '-')],
}
VA_RULES = {
    'relax': [('arousal', '-'), ('valence', '+')],
    'concentrate': [('arousal', '-'), ('valence', 'mid')],
    'cruise': [('arousal', 'mid'), ('valence', '+')],
    'energize': [('arousal', '+')], 'uplift': [('valence', '+')],
    'release': [('arousal', '+'), ('valence', '-')],
}
ALIASES = {'emotify': {'calm': 'calmness', 'joy': 'joyful_activation'},
           'emma-preview-matched': {'calm': 'peacefulness', 'joy': 'joy'}}
ACTIVITIES = {'concentrate': ['Usage-At_work', 'Usage-Reading', 'Usage-Studying'],
              'energize': ['Usage-Exercising']}


def percentiles(reference, values):
    """Midpoint empirical ranks; reference predictions contain no gold inputs."""
    return np.column_stack([(np.searchsorted(np.sort(ref), col, side='left')
                             + np.searchsorted(np.sort(ref), col, side='right')) / (2 * len(ref))
                            for ref, col in zip(reference.T, values.T)])


def semantic_scores(values, names, rules, aliases):
    def term(name, direction):
        column = values[:, names.index(aliases.get(name, name))]
        return column if direction == '+' else (1 - column if direction == '-' else 1 - 2 * np.abs(column - .5))
    return np.column_stack([np.mean([term(*item) for item in rules[vibe]], axis=0) for vibe in VIBES])


def predict_export(artifact, embedding):
    plan = artifact['plan']
    aliases = plan['aliases']
    mapped, activity = {}, {}
    for source, head in artifact['native_heads'].items():
        model = {key: np.asarray(value) if isinstance(value, list) else value for key, value in head['model'].items()}
        raw = predict(model, embedding)[:, head['selected_columns']]
        ranked = percentiles(np.asarray(artifact['references'][source]), raw)
        names = [head['target_names'][i] for i in head['selected_columns']]
        if source == 'cal500':
            activity = {vibe: ranked[:, [names.index(name) for name in labels]].mean(1)
                        for vibe, labels in plan['activity_support'].items()}
        else:
            rules = plan['gems_rules'] if source in aliases else plan['va_rules']
            mapped[source] = semantic_scores(ranked, names, rules, aliases.get(source, {}))
    result = dict(mapped)
    gems = [value for source, value in mapped.items() if source in aliases]
    va = [value for source, value in mapped.items() if source not in aliases]
    result['equal_gems'] = np.mean(gems, axis=0)
    result['equal_va'] = np.mean(va, axis=0)
    result['equal_all'] = np.mean(list(mapped.values()), axis=0)
    combined = result['equal_all'].copy()
    for vibe, values in activity.items():
        combined[:, VIBES.index(vibe)] = .5 * (combined[:, VIBES.index(vibe)] + values)
    result['equal_all_activity_support'] = combined
    return result


def evaluate():
    with threadpool_limits(limits=1):
        rows = [row for row in json.loads(SNAPSHOT.read_text())['gold'] if len(row.get('embedding_values', [])) == 200]
        sources = ['emotify', 'pmemo', 'deam', 'cal500']
        if (OUT / 'emma-preview-matched.report.json').exists():
            sources.append('emma-preview-matched')
        plan = {'created_at': stamp(), 'sources': sources, 'gems_rules': GEMS_RULES, 'va_rules': VA_RULES,
                'aliases': ALIASES, 'activity_support': ACTIVITIES, 'script_sha256': digest(__file__),
                'coefficient_policy': 'Equal terms within each fixed semantic rule; equal source votes; activity-support variant averages in direct CAL500 studying/reading/work and exercise predictions for those two vibes only. No gold-label parameter fitting or mixture selection.'}
        save(OUT / 'semantic-fixed.plan.json', plan)
        _, _, exclusions, signatures, heads = external_head_features(sources, rows)
        references = {}
        for source, head in heads.items():
            retained = [row for row in json.loads((OUT / f'{source}.features.json').read_text())['rows']
                        if row['id'] not in set(exclusions[source])]
            x = np.array([json.loads(Path(row['feature_file']).read_text())['prediction']['embedding']['values'] for row in retained])
            model = {key: np.asarray(value) if isinstance(value, list) else value for key, value in head['model'].items()}
            references[source] = predict(model, x)[:, head['selected_columns']]
        artifact = {'format': 'external-research-fixed-semantics-1', 'vibes': VIBES, 'plan': plan,
                    'native_heads': heads, 'references': serializable(references),
                    'gold_training_ids': [], 'embedding_signatures': sorted(signatures)}
        values = predict_export(artifact, np.array([row['embedding_values'] for row in rows]))
        save(OUT / 'models/semantic-fixed.json', artifact)
        # Human labels enter only after every fixed prediction is computed.
        y = np.array([[int(name in row['labels']) for name in VIBES] for row in rows], dtype=float)
        report = {'created_at': stamp(), 'rows': len(rows), 'sources': sources, 'plan_sha256': digest(OUT / 'semantic-fixed.plan.json'),
                  'snapshot_sha256': digest(SNAPSHOT), 'gold_training_rows': 0,
                  'methods': {name: metrics(y, value) for name, value in values.items()},
                  'external_identity_exclusions': exclusions,
                  'validation': 'No developer vibe labels enter native heads, reference percentiles, semantic rules, or source weighting. Gold is scored only after frozen predictions are produced.',
                  'limitations': ['The same previously inspected developer snapshot is exploratory; fixed rules were designed after earlier experiments and are not a preregistered independent benchmark.',
                                 'Semantic mappings are explicit, approximate assumptions. Calmness alone does not establish concentration, tension is not necessarily catharsis, and activity support is incomplete.',
                                 'Percentile scores are within-source ranking scales, not calibrated suitability probabilities. Binary log loss is descriptive only.',
                                 'Available external annotations and unknown upstream pretraining overlaps retain their original limitations.']}
        save(OUT / 'semantic-fixed.report.json', report)
        save(OUT / 'semantic-fixed.predictions.json', {'ids': [row['video_id'] for row in rows], 'observed': y.tolist(),
             'predictions': {name: value.tolist() for name, value in values.items()}})
        print(json.dumps({'fixed_semantics': report['methods']}), flush=True)
        return report


if __name__ == '__main__':
    evaluate()
