"""Freeze, fit and audit #614 without changing production or prior experiments."""
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import shutil
import sys

import numpy as np
from threadpoolctl import threadpool_limits

from . import individual_protocol as protocol
from . import release_models as models
from .external_experiment import DATA, LAB, digest, save, stamp, table
from .external_sources import JAMENDO, JAMENDO_SOURCE, JAMENDO_TASKS, jamendo_annotations
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load, runtime
from .individual_experiment import compact, receipt

PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
DEFAULT = LAB / 'local/release-614-20261030/round-2'
LOCAL = LAB / 'local/release-614-20261030/local-1'
EXPERIMENTS = LAB.parent
JAM, EMO, SOUND = 'jamendo-human-4shards', 'emotify-artist', 'soundtracks-all'
SOURCES = (JAM, EMO, SOUND)
TARGET = {JAM: 'aggressive', EMO: 'power', SOUND: 'anger'}
ATTRIBUTES = {JAM: ['happy', 'party', 'relaxed', 'sad'],
              EMO: ['joyful_activation', 'tension', 'calmness', 'sadness'],
              SOUND: ['energy', 'tension', 'valence', 'happy', 'fear', 'sad', 'tender']}


def raw_paths():
    return [JAMENDO_SOURCE / 'derived/music-classification-annotations/music-classification-annotations-raw.tsv',
            JAMENDO_SOURCE / 'README.md', JAMENDO_SOURCE / 'audio_licenses.txt',
            JAMENDO_SOURCE / 'data/download/autotagging_moodtheme_audio-low_sha256_tracks.txt',
            DATA / 'emotify/data.csv', DATA / 'soundtracks/mean_ratings_set1.csv',
            DATA / 'soundtracks/mean_ratings_set2.csv', DATA / 'soundtracks/readme.md']


def audit(directory):
    if directory.exists():
        raise ValueError('Preserve the completed audit; choose a new directory')
    result = {'created_at': stamp(), 'sources': {}, 'raw_references': {str(p): digest(p) for p in raw_paths()}}
    annotations = {row['id']: row for row in jamendo_annotations()}
    emotify = defaultdict(list)
    for row in table(DATA / 'emotify/data.csv'):
        emotify['emotify:' + row['track id']].append(row)
    sound = {f'soundtracks:set{s}:{int(row["number"]):03d}': row
             for s in (1, 2) for row in table(DATA / f'soundtracks/mean_ratings_set{s}.csv')}
    for source in SOURCES:
        meta = load(PREVIOUS / f'inputs/{source}.json')
        data = arrays(PREVIOUS / f'inputs/{source}.npz')
        index = meta['target_names'].index(TARGET[source])
        counts, mixed, reconstructed = [], [], []
        for row in meta['rows']:
            if source == JAM:
                item = annotations[row['id']]
                values = item['targets']
                count = item['rater_counts'][JAMENDO_TASKS.index('aggressive')]
                counts.append(count)
                mixed.append(0 < values[index] < 1)
            elif source == EMO:
                original = emotify[row['id']]
                values = [np.mean([float(r[name]) for r in original]) for name in meta['target_names']]
                counts.append(len(original))
                mixed.append(0 < values[index] < 1)
            else:
                values = [(float(sound[row['id']][name]) - 1) / 8 for name in meta['target_names']]
            reconstructed.append(values)
        if not np.allclose(reconstructed, data['y'], atol=1e-12, rtol=0):
            raise ValueError('Original annotations do not reproduce frozen labels: ' + source)
        y = data['y'][:, index]
        result['sources'][source] = {
            'native_target': TARGET[source], 'rows': len(y), 'groups': len(set(meta['groups'])),
            'previous_protected_rows': int(data['transfer_keep'].sum()),
            'native_scale': [1, 9] if source == SOUND else [0, 1],
            'stored_scale': [0, 1], 'mean': float(y.mean()),
            'zero_rows': int((y == 0).sum()), 'high_at_least_065': int((y >= .65).sum()),
            'low_at_most_035': int((y <= .35).sum()),
            'target_values': sorted(set(y.tolist())) if source == JAM else None,
            'responses_min_median_max': [min(counts), float(np.median(counts)), max(counts)] if counts else None,
            'responses': sum(counts) if counts else None, 'mixed_vote_rows': int(sum(mixed)) if counts else None,
            'annotation_units': dict(Counter(row['annotation_unit'] for row in meta['rows'])),
            'audio_feature_seconds': dict(Counter(row['sample_seconds'] for row in meta['rows'])),
            'coannotated_diagnostic_inputs': ATTRIBUTES[source],
            'missing_artist_names': sum(not r.get('artist') for r in meta['rows']),
            'rater_limits': 'Counts/disagreement describe supplied votes; repeated-listener identities are not available. Soundtracks supplied files contain means, without per-item rater counts.',
        }
    known = load(PREVIOUS / f'inputs/{JAM}.json')['rows']
    known_ids = {row['id'] for row in known}
    known_artists = {row['group'].removeprefix('jamendo:') for row in known}
    hashes = dict(line.strip().split(maxsplit=1)[::-1] for line in raw_paths()[3].read_text().splitlines() if line.strip())
    supplied = {key.lstrip('*') for key in hashes}
    unused = [row for row in annotations.values() if row['id'] not in known_ids
              and row['artist'] not in known_artists and row['path'].replace('.mp3', '.low.mp3') in supplied]
    next_shards = ['01', '02']
    proposed = [row for row in unused if row['path'].split('/')[0] in next_shards]
    # Do not select by labels or scores. These identities are an acquisition inventory,
    # not a newly claimed holdout; no candidate is evaluated on them in this round.
    result['unused_jamendo'] = {
        'all_complete_human_annotations': len(annotations),
        'verified_local_human_audio': len(list((JAMENDO / 'human-audio').rglob('*.mp3'))),
        'current_feature_rows': len(known),
        'unused_known_artist_disjoint_published_audio': len(unused),
        'unused_known_artist_disjoint_groups': len({r['artist'] for r in unused}),
        'next_acquisition': {'shards': next_shards, 'rows': len(proposed),
            'artists': len({r['artist'] for r in proposed}), 'ids': [r['id'] for r in proposed],
            'selection': 'First two unused numerical shards, all explicit-human/checksum matches outside known training artists; no label/score selection.',
            'bounds': 'At most these two official shards and 1.25 GiB combined transfer; verify per-track published SHA256 and retain missing rows. A later round must freeze selection, candidates and protocol before acquisition/evaluation.',
            'limits': 'New audio rows from a known corpus, not an independent annotation population; unknown cross-corpus aliases/backbone exposure remain.'},
    }
    result['semantic_bridge'] = {
        'direct_three_construct_coannotation': 'None in the supplied native schemas: no primary source labels the same excerpt with both aggression and power or anger.',
        'available': 'Within-source emotion/activation co-annotations can diagnose each native target, not prove the three targets equivalent or measure cathartic benefit.',
        'weak_controls': {'coimbra': 'Tag-derived class, already retained in #577.',
                          'emma': 'Power labels refer to a longer, unverified rated excerpt; already retained in #577.'}}
    save(directory / 'audit.json', result)
    print(json.dumps({'audit': str(directory), 'counts': {s: result['sources'][s]['rows'] for s in SOURCES},
                      'unused_audio_matches': len(unused), 'next_two_shards': len(proposed)}), flush=True)


def dataset(directory, source):
    data = arrays(directory / f'inputs/{source}.npz')
    meta = load(directory / f'inputs/{source}.json')
    keep = data['release_keep']
    return {'x': data['embedding'][keep], 'y': data['y'][keep, meta['target_names'].index(TARGET[source])],
            'native': data['y'][keep], 'groups': np.asarray(meta['groups'])[keep],
            'ids': np.asarray([r['id'] for r in meta['rows']])[keep], 'outer': data['outer'][keep]}


def fold_tree(data, depth=3):
    assignment = models.split(data)
    value = {'ids': data['ids'].tolist(), 'folds': assignment.tolist()}
    if depth > 1:
        value['training_subtrees'] = {str(f): fold_tree(models.subset(data, assignment != f), depth - 1)
                                     for f in sorted(set(assignment))}
    return value


def code_paths():
    from . import release_evaluation  # noqa: F401
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if name.startswith('experiments.audio_lab_tools.') and getattr(m, '__file__', None)}
    paths.update((Path(__file__).resolve(), EXPERIMENTS / 'run.py',
                  LAB / 'RELEASE_MAPPING_PLAN.md', Path(__file__).with_name('test_release_mapping.py'),
                  Path(__file__).with_name('test_release_results.py'),
                  Path(__file__).with_name('test_individual_results.py')))
    return {str(p.relative_to(EXPERIMENTS)): p for p in sorted(paths)}


def freeze(directory, audit_directory):
    from .individual_experiment import validate as previous_validate
    if directory.exists():
        raise ValueError('Preserve previous evidence; choose a new directory')
    prior = previous_validate(PREVIOUS)
    check_receipt(PREVIOUS, 'evaluation.receipt.json')
    check_receipt(LOCAL, 'receipt.json', False)
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    ref(PREVIOUS / 'plan.json')
    ref(PREVIOUS / 'evaluation.receipt.json')
    audit_value = load(ref(audit_directory / 'audit.json'))
    for path, checksum in audit_value['raw_references'].items():
        if digest(ref(path)) != checksum:
            raise ValueError('Raw audit input changed')
    ref(LOCAL / 'receipt.json')
    local = load(ref(LOCAL / 'identities.json'))
    ref(LOCAL / 'labels.json')
    for name in load(LOCAL / 'receipt.json')['outputs']:
        ref(LOCAL / name)
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    exclusions, retained_rows = {}, {}
    for source in SOURCES:
        check_receipt(PREVIOUS, 'fit-' + source + '.receipt.json')
        ref(PREVIOUS / ('fit-' + source + '.receipt.json'))
        data = arrays(ref(PREVIOUS / f'inputs/{source}.npz'))
        meta = load(ref(PREVIOUS / f'inputs/{source}.json'))
        keep, exclusion = protocol.exclude(meta['rows'], local)
        data['release_keep'] = data['transfer_keep'] & keep
        data['evaluation_keep'] = data['native_keep'] & keep
        exclusions[source] = exclusion
        if source == SOUND:
            data['common'] &= keep
        retained_rows[source] = [r for i, r in enumerate(meta['rows']) if data['release_keep'][i]]
        protocol.group_guard(np.asarray(meta['groups'])[data['release_keep']], data['outer'][data['release_keep']])
        save(directory / f'inputs/{source}.json', meta)
        np.savez_compressed(directory / f'inputs/{source}.npz', **data)
        for relative in (f'models/{source}.embedding.json', source + '.native-oof.npz'):
            original = ref(PREVIOUS / relative)
            shutil.copyfile(original, directory / 'inputs' / original.name)
    for source in SOURCES:
        protected = [r for other, rows in retained_rows.items() if other != source for r in rows]
        keep, excluded = protocol.exclude(retained_rows[source], protected)
        if not keep.all():
            raise ValueError('Protected native sources share known identities: ' + source)
    save(directory / 'inputs/exclusions.json', exclusions)
    save(directory / 'inputs/audit.json', audit_value)
    for relative in ('primary.predictions.npz', 'primary.report.json', 'reciprocal.report.json',
                     'reciprocal.predictions.npz', 'inputs/checkpoint.json'):
        original = ref(PREVIOUS / relative)
        shutil.copyfile(original, directory / 'inputs' / original.name)
    data = {s: dataset(directory, s) for s in SOURCES}
    trees = {s: {'full': fold_tree(d), 'outer_training': {
        str(f): fold_tree(models.subset(d, d['outer'] != f)) for f in range(5)}} for s, d in data.items()}
    save(directory / 'inputs/folds.json', trees)
    paths = code_paths()
    for relative, path in paths.items():
        target = directory / 'source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    plan = {'created_at': stamp(), 'ticket': 614, 'runtime': runtime(),
            'previous_plan_sha256': digest(PREVIOUS / 'plan.json'),
            'seed': models.SEED, 'sources': SOURCES, 'targets': TARGET, 'diagnostic_attributes': ATTRIBUTES,
            'mapping_weights': models.WEIGHTS, 'native_task_penalties': models.PENALTIES,
            'native_selector': [{'family': 'mean'}, *protocol.SPECS],
            'references': references, 'local': str(LOCAL),
            'code': {name: digest(path) for name, path in paths.items()},
            'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
            'embedding_signature': prior['embedding_signature'], 'attribute_names': prior['attribute_names'],
            'jamendo_new_artifacts': prior['jamendo_new_artifacts'],
            'bootstrap_draws': 1000, 'native_rows': {s: len(d['y']) for s, d in data.items()},
            'budgets': {'single_bytes': 262144, 'mapping_bytes': 524288, 'warm_three_ms': 100, 'rss_bytes': 1073741824},
            'acceptance': {'primary_ndcg_gain_over_jamendo': .02, 'primary_lift_gain_over_jamendo': .01,
                           'positive_paired_lower_bounds': True, 'also_beat_incumbent': True,
                           'material_reciprocal_ndcg_loss': .02, 'material_reciprocal_lift_loss': .01,
                           'independent_confirmation_available': False},
            'exposure': 'Previously inspected native populations; no new confirmation audio acquired. Local labels supplementary.'}
    save(directory / 'plan.json', plan)
    validate(directory, execution=True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'native_rows': plan['native_rows']}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    if execution and plan['runtime'] != runtime():
        raise ValueError('Numerical runtime changed')
    for relative, checksum in plan['inputs'].items():
        if digest(directory / relative) != checksum:
            raise ValueError('Frozen input changed: ' + relative)
    for relative, checksum in plan['code'].items():
        if digest(directory / 'source' / relative) != checksum:
            raise ValueError('Archived source changed: ' + relative)
        if execution and digest(EXPERIMENTS / relative) != checksum:
            raise ValueError('Execution code changed; preserve round: ' + relative)
    for path, checksum in plan['references'].items():
        if digest(path) != checksum:
            raise ValueError('Pinned reference changed: ' + path)
    return plan


def fit_native(directory):
    if (directory / 'fit-native.receipt.json').exists():
        check_receipt(directory, 'fit-native.receipt.json')
        return
    data = {s: dataset(directory, s) for s in SOURCES}
    scores = {s: {method: np.empty(len(d['y'])) for method in (
        'anchor', 'monotone', 'zero_weight', 'native_tasks', 'training_mean', 'training_median',
        'observed_attributes', 'predicted_attributes')} for s, d in data.items()}
    traces = {}
    for fold in range(5):
        training = {s: models.subset(d, d['outer'] != fold) for s, d in data.items()}
        joint, joint_trace = models.tasks_select(training)
        traces[str(fold)] = {'native_tasks': joint_trace, 'sources': {}}
        for source in SOURCES:
            valid = data[source]['outer'] == fold
            x = data[source]['x'][valid]
            auxiliary = EMO if source != EMO else JAM
            anchor, anchor_trace = models.native_fit(training[source])
            model, trace = models.mapping_fit(training[source], training[auxiliary])
            prediction = scores[source]
            prediction['anchor'][valid] = models.native_predict(anchor, x)
            prediction['monotone'][valid] = models.mapping_predict(model, x)
            prediction['zero_weight'][valid] = models.mapping_predict(model, x, zero=True)
            prediction['native_tasks'][valid] = models.tasks_predict(joint, x, source)
            prediction['training_mean'][valid] = training[source]['y'].mean()
            prediction['training_median'][valid] = np.median(training[source]['y'])
            meta = load(directory / f'inputs/{source}.json')
            columns = [meta['target_names'].index(name) for name in ATTRIBUTES[source]]
            mapping_data = {**training[source], 'x': training[source]['native'][:, columns]}
            semantic_head, semantic_trace = models.native_fit(mapping_data)
            attribute_heads, attribute_trace = protocol.select(training[source]['x'],
                training[source]['native'][:, columns], models.split(training[source]))
            observed = data[source]['native'][valid][:, columns]
            estimated = head_predictions(attribute_heads, x)
            prediction['observed_attributes'][valid] = models.native_predict(semantic_head, observed)
            prediction['predicted_attributes'][valid] = models.native_predict(semantic_head, estimated)
            traces[str(fold)]['sources'][source] = {'anchor': anchor_trace, 'mapping': trace,
                'semantic_mapping': semantic_trace, 'native_attributes': attribute_trace}
            print(json.dumps({'native_fold': fold, 'source': source, 'weight': model['weight'],
                              'joint_penalty': joint['penalty']}), flush=True)
    outputs = []
    for source, values in scores.items():
        path = directory / (source + '.native.npz')
        np.savez_compressed(path, **values)
        outputs.append(path)
    path = directory / 'native.fit.json'
    save(path, traces)
    outputs.append(path)
    receipt(directory, 'fit-native', outputs)


def fit_transfer(directory, target):
    stage = 'fit-transfer-' + target
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    training = {s: dataset(directory, s) for s in SOURCES if s != target}
    anchor = JAM if JAM in training else SOUND
    auxiliary = next(s for s in training if s != anchor)
    native, native_trace = models.native_fit(training[anchor])
    mapping, mapping_trace = models.mapping_fit(training[anchor], training[auxiliary])
    joint, joint_trace = models.tasks_select(training)
    value = {'format': 'release-614-v1', 'target_excluded': target, 'anchor_source': anchor,
             'auxiliary_source': auxiliary, 'anchor': native, 'mapping': mapping, 'native_tasks': joint,
             'plan_sha256': digest(directory / 'plan.json'),
             'training_ids': {s: d['ids'].tolist() for s, d in training.items()}}
    path = directory / f'models/transfer-{target}.json'
    compact(path, value)
    trace_path = directory / f'transfer-{target}.fit.json'
    save(trace_path, {'anchor': native_trace, 'mapping': mapping_trace, 'native_tasks': joint_trace})
    receipt(directory, stage, [path, trace_path])
    print(json.dumps({'transfer_target': target, 'training': list(training), 'anchor': anchor,
                      'weight': mapping['weight'], 'joint_penalty': joint['penalty']}), flush=True)


def main():
    from . import release_evaluation
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit', 'freeze', 'fit', 'evaluate', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--audit-directory', type=Path, default=DEFAULT.parent / 'audit-1')
    args = parser.parse_args()
    directory, audit_directory = inside(args.directory), inside(args.audit_directory)
    with threadpool_limits(limits=1):
        if args.action == 'audit':
            audit(audit_directory)
        elif args.action == 'freeze':
            freeze(directory, audit_directory)
        elif args.action == 'fit':
            validate(directory, execution=True)
            fit_native(directory)
            for target in SOURCES:
                fit_transfer(directory, target)
            validate(directory, execution=True)
        elif args.action == 'evaluate':
            release_evaluation.run(directory)
        else:
            validate(directory)
            for name in ('fit-native', *('fit-transfer-' + s for s in SOURCES), 'evaluation'):
                check_receipt(directory, name + '.receipt.json')
            print(json.dumps({'verified': str(directory)}), flush=True)


if __name__ == '__main__':
    main()
