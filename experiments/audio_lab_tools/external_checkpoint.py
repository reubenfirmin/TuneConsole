"""Freeze research inputs and run the first #530 experiments sequentially.

All outputs stay under experiments/. The application database is read-only;
heavy work keeps its experiment resource bounds independently of pytest.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import traceback

from threadpoolctl import threadpool_limits

from .external_experiment import LAB, MODEL, OUT, ROOT, digest, save, stamp
from .external_secondary import identity

RUN = OUT / 'checkpoint-1'
READY = ('soundtracks-all', 'emotify-artist', 'cal500-raw-activity', 'pmemo',
         'vgmidi-original', 'jamendo-human-4shards')
CORE = (*READY, 'deam')
STEPS = ('emotify-identities', 'snapshot', 'rater-weights', 'cal500-ranking', 'local-pool',
         'emotify-plus', 'merp', 'propagation', 'local-soundtracks', 'local-pmemo',
         'native-ready', 'secondary', 'learners-ready', 'workflow', 'reference', 'temporal-pmemo',
         'deam', 'temporal-deam', 'learners-deam', 'cross-corpus', 'emma', 'emotify-windows',
         'merge', 'merge-expansion', 'overlaps', 'consensus-core', 'consensus-selected', 'ranking-core', 'semantics-core',
         'projection', 'multitask', 'exports')


def protected_identities():
    manifest = json.loads((RUN / 'inputs/manifest.json').read_text())
    path = RUN / 'inputs/human-labels.json'
    if digest(path) != manifest['files']['human-labels.json']:
        raise ValueError('Reserved human snapshot changed')
    return [{key: row.get(key, '') for key in ('video_id', 'artist', 'title')}
            for row in json.loads(path.read_text())['gold']]


def dictionaries(conn, sql):
    return [dict(row) for row in conn.execute(sql)]


def freeze(database):
    from . import train_vibes
    from .external_training import folds
    from .external_transfer import SNAPSHOT
    from yt_playlist.core import audio_annotations, sample_files

    target = RUN / 'inputs'
    if (target / 'manifest.json').exists():
        manifest = json.loads((target / 'manifest.json').read_text())
        for filename, expected in manifest['files'].items():
            if digest(target / filename) != expected:
                raise ValueError('Frozen input changed: ' + filename)
        return manifest
    target.mkdir(parents=True, exist_ok=True)
    started = stamp()
    # This export pins human labels, catalog features and their selected audio
    # inputs together in the trainer's read transaction. A second transaction
    # freezes prediction lineage and the wider capture pool independently.
    gold, skipped = train_vibes.examples_from_db(database, require_embeddings=True)
    historical = json.loads(SNAPSHOT.read_text())
    old_ids = {row['video_id'] for row in historical['gold']}
    old_silver = {row['video_id'] for row in historical['silver']}
    additions = [row['video_id'] for row in gold if row['video_id'] not in old_ids]
    save(target / 'human-labels.json', {'gold': gold, 'skipped': skipped,
         'historical_snapshot_sha256': digest(SNAPSHOT),
         'historical_cohort_ids': sorted(old_ids), 'new_label_cohort_ids': additions,
         'new_labels_previously_in_silver': sorted(set(additions) & old_silver),
         'policy': 'New-label cohort is reserved from model selection in this checkpoint. Prior weak-label or representation exposure must be audited before calling it prospective.'})

    copied, missing, sample_rows = {}, [], []
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        samples = dictionaries(conn, 'SELECT * FROM sample_attributes ORDER BY captured_at,id')
        annotation_snapshot = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audio_annotations'").fetchone():
            labels, _ = audio_annotations.training_rows(conn, train_vibes.TAXONOMY_VERSION)
            annotation_snapshot = audio_annotations.export_snapshot(conn, target / 'annotation-documents')
        else:
            labels = dictionaries(conn, 'SELECT * FROM audio_session_labels ORDER BY updated_at,run_id')
        library = {'captured_at': stamp(), 'labels': labels,
            'recordings': dictionaries(conn, 'SELECT id,identity_key,title,artist FROM recordings '
                'WHERE id IN (SELECT recording_id FROM recording_vibes UNION SELECT recording_id FROM sample_attributes)'),
            'recording_vibes': dictionaries(conn, 'SELECT * FROM recording_vibes ORDER BY recording_id,vibe'),
            'predictions': dictionaries(conn, 'SELECT * FROM predictions WHERE id IN '
                '(SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL) ORDER BY id'),
            'prediction_inputs': dictionaries(conn, 'SELECT i.* FROM prediction_inputs i WHERE prediction_id IN '
                '(SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL) ORDER BY prediction_id,measurement_id'),
            'prediction_artists': dictionaries(conn, 'SELECT a.* FROM prediction_artists a WHERE prediction_id IN '
                '(SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL) ORDER BY prediction_id,artist_id'),
            'measurements': dictionaries(conn, 'SELECT * FROM measurements WHERE id IN '
                '(SELECT measurement_id FROM recording_vibes WHERE measurement_id IS NOT NULL UNION '
                'SELECT measurement_id FROM prediction_inputs WHERE prediction_id IN '
                '(SELECT prediction_id FROM recording_vibes WHERE prediction_id IS NOT NULL)) ORDER BY id')}
        for row in samples:
            try:
                value = sample_files.read(row['filename'], expected={k: row[k] for k in ('id', 'run_id', 'point', 'video_id')})
                source = Path(row['filename'])
                relative = 'samples/' + source.name
                destination = target / relative
                destination.parent.mkdir(exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source, destination)
                copied[relative] = digest(destination)
                sample_rows.append({**row, 'filename': str(destination.resolve()),
                                    'source_filename': row['filename'],
                                    'has_embedding': value.get('embedding') is not None})
            except (OSError, ValueError) as error:
                missing.append({'id': row['id'], 'error': str(error)})
        library['sample_attributes'] = sample_rows
        if annotation_snapshot is not None:
            library['annotations'] = annotation_snapshot
    save(target / 'library.json', library)

    # Bind all available corpus versions by source group, recording ID,
    # encoded audio hash and known artist name. Missing artists stay unknown.
    parents, links, indexed, sources = {}, {}, [], {}

    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]

    for path in sorted(OUT.glob('*.features.json')):
        source = json.loads(path.read_text())
        if not source.get('rows'):
            continue
        dataset = path.name.removesuffix('.features.json')
        (target / 'external').mkdir(exist_ok=True)
        shutil.copyfile(path, target / 'external' / path.name)
        sources[dataset] = {'rows': len(source['rows']), 'sha256': digest(path),
                            'errors': len(source.get('errors', []))}
        for row in source['rows']:
            key = dataset + '/' + row['id']
            tokens = [('group', row['group']), ('recording', row['id'])]
            if row.get('audio_sha256'):
                tokens.append(('audio', row['audio_sha256']))
            if identity(row.get('artist', '')):
                tokens.append(('artist', identity(row['artist'])))
            for token in tokens:
                if token in links:
                    parents[find(key)] = find(links[token])
                else:
                    links[token] = key
            indexed.append({'dataset': dataset, 'id': row['id'], 'key': key,
                            'artist_known': bool(identity(row.get('artist', '')))})
    groups = [find(row['key']) for row in indexed]
    assignments = folds(groups)
    save(target / 'external-groups.json', {'sources': sources,
         'rows': [{**row, 'group': group, 'fold': int(fold)}
                  for row, group, fold in zip(indexed, groups, assignments)],
         'policy': 'Global artist/source-group/recording/encoded-audio connected components; five folds. Correlated corpus versions remain alternative tasks. Existing native results retain their original splits.',
         'limitations': ['Encoded hashes do not detect different encodings or masters.',
                        'Missing artist names, aliases and unparsed collaborative credits limit artist separation.',
                        'This manifest describes acquired inputs; partial extraction is not a completed benchmark.']})
    sources_by_recording = {}
    for row in library['recording_vibes']:
        sources_by_recording.setdefault(row['source'], set()).add(row['recording_id'])
    manifest = {'created_at': stamp(), 'started_at': started, 'database': str(database.resolve()),
        'database_access': 'read-only; human feature export and lineage export use separately pinned read transactions',
        'human_rows_with_embeddings': len(gold), 'new_label_rows': len(additions),
        'sample_rows': len(sample_rows), 'captured_recordings': len({r['recording_id'] for r in sample_rows}),
        'recordings_with_embeddings': len({r['recording_id'] for r in sample_rows if r['has_embedding']}),
        'current_source_recordings': {k: len(v) for k, v in sources_by_recording.items()},
        'predictions': len(library['predictions']),
        'predictions_without_complete_lineage': sum(not r['lineage_complete'] for r in library['predictions']),
        'unavailable_sample_files': missing, 'sources': sources,
        'files': {**copied, **{str(p.relative_to(target)): digest(p) for p in target.rglob('*.json')
                              if p.name != 'manifest.json' and p.parent.name != 'samples'}},
        'source_code_sha256': digest(__file__)}
    save(target / 'manifest.json', manifest)
    print(json.dumps({'frozen_inputs': {k: manifest[k] for k in (
        'human_rows_with_embeddings', 'new_label_rows', 'captured_recordings',
        'recordings_with_embeddings', 'predictions', 'predictions_without_complete_lineage')}}), flush=True)
    return manifest


def fingerprint(step):
    paths = [*Path(__file__).parent.glob('external_*.py'),
             LAB / 'vendor/bundle.json', LAB / 'vendor/runtime.json',
             RUN / 'inputs/manifest.json', Path(__file__).with_name('runtime_snapshot.py')]
    dependencies = {
        'emotify-identities': ('emotify.features.json',),
        'rater-weights': ('emotify-artist.features.json', 'models/emotify-artist.embedding.json'),
        'cal500-ranking': ('cal500-raw-activity.features.json', 'cal500-raw-activity.oof.csv'),
        'emotify-windows': ('emotify-artist.features.json',),
        'deam': ('deam.index.json',), 'emma': ('emma-preview-matched.index.json',),
        'emotify-plus': ('emotify-artist.features.json',),
        'merp': ('../datasets/merp/acquisition.json',),
        'local-pool': ('checkpoint-1/inputs/library.json', 'checkpoint-1/inputs/human-labels.json'),
        'propagation': ('checkpoint-1/inputs/library.json', 'checkpoint-1/inputs/human-labels.json'),
        'local-soundtracks': ('soundtracks-all.features.json', 'checkpoint-1/local-audio-pool.json'),
        'local-pmemo': ('pmemo.features.json', 'checkpoint-1/local-audio-pool.json'),
        'native-ready': tuple(name + '.features.json' for name in READY),
        'learners-ready': tuple(name + '.features.json' for name in READY),
        'learners-deam': ('deam.features.json',),
        'secondary': tuple(name + '.features.json' for name in ('coimbra-mirex-like', '4q-emotion', 'cal500', 'pmemo')),
        'temporal-pmemo': ('pmemo.features.json',), 'temporal-deam': ('deam.features.json',),
        'cross-corpus': tuple(name + '.features.json' for name in ('pmemo', 'deam', 'amg1608-matched', 'ntwicm-matched')),
        'merge': ('../datasets/merge/acquisition.json',),
        'merge-expansion': ('merge-quadrants.features.json', '4q-emotion.features.json'),
    }
    paths.extend(OUT / name for name in dependencies.get(step, ()))
    if step == 'emotify-plus':
        paths.append(LAB / 'local/datasets/emotify-plus/musicemotions_dataset_v2.csv')
    if step == 'propagation':
        from yt_playlist.rec import vibe_prediction
        paths.append(Path(vibe_prediction.__file__))
    if step in ('consensus-core', 'consensus-selected', 'ranking-core', 'semantics-core', 'projection', 'multitask', 'exports'):
        paths.extend(OUT / (name + '.features.json') for name in CORE)
        paths.extend(OUT / 'models' / (name + '.embedding.json') for name in CORE)
        paths.append(RUN / 'inputs/human-labels.json')
    if step == 'workflow':
        from .external_workflow import runtime_hashes
        paths.extend(ROOT / name for name in runtime_hashes())
        paths.append(ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json')
    if step == 'overlaps':
        paths.extend(OUT.glob('*.features.json'))
    if step == 'reference':
        paths.append(Path(__file__).with_name('reference_pipeline.py'))
        paths.append(RUN / 'reference-pipeline/acquisition.json')
    payload = {str(p.relative_to(ROOT)): digest(p) for p in paths if p.exists()}
    return hashlib.sha256(json.dumps({'step': step, 'inputs': payload}, sort_keys=True).encode()).hexdigest()


def outputs(step):
    names = {
        'emotify-identities': ('emotify-artist.features.json', 'emotify-artist.identity-audit.json', 'emotify-artist.report.json'),
        'snapshot': ('checkpoint-1/inputs/manifest.json',),
        'rater-weights': ('emotify-artist.rater-weights.report.json', 'emotify-artist.rater-weights.oof.json'),
        'cal500-ranking': ('cal500-raw-activity.ranking.json',),
        'emotify-windows': ('emotify-artist-first.features.json', 'emotify-artist-last.features.json',
                            'emotify-artist-multiwindow.features.json', 'emotify-artist-multiwindow.report.json'),
        'deam': ('deam.features.json', 'deam.report.json'),
        'emma': ('emma-preview-matched.features.json', 'emma-preview-matched.report.json'),
        'emotify-plus': ('emotify-plus-strict.features.json', 'emotify-plus-strict.join-audit.json',
                         'emotify-plus-strict.report.json'),
        'merp': ('merp-static.features.json', 'merp-static.join-audit.json', 'merp-static.report.json'),
        'local-pool': ('checkpoint-1/library-audit.json', 'checkpoint-1/local-audio-pool.json'),
        'propagation': ('checkpoint-1/propagation.report.json', 'checkpoint-1/propagation-human.oof.json'),
        'native-ready': tuple(name + '.report.json' for name in READY),
        'learners-ready': tuple(name + suffix for name in READY for suffix in ('.learners.report.json', '.learners.oof.json')),
        'learners-deam': ('deam.learners.report.json', 'deam.learners.oof.json'),
        'secondary': tuple(name + suffix for name in ('amg1608-matched', 'ntwicm-matched')
                           for suffix in ('.features.json', '.join-audit.json', '.report.json')),
        'temporal-pmemo': ('pmemo.temporal-comparison.json', 'pmemo-dynamic.report.json', 'pmemo-dynamic.learners.report.json'),
        'temporal-deam': ('deam.temporal-comparison.json', 'deam-dynamic.report.json', 'deam-dynamic.learners.report.json'),
        'cross-corpus': ('cross-corpus.report.json',),
        'merge': ('merge-quadrants.features.json', 'merge-quadrants.report.json', 'merge-tag-va.features.json', 'merge-tag-va.report.json'),
        'merge-expansion': ('merge-4q-expansion.report.json', 'merge-4q-expansion.oof.json'),
        'workflow': ('checkpoint-1/listening-workflow-index.json',),
        'reference': ('checkpoint-1/reference-pipeline/report.json',),
        'overlaps': ('checkpoint-1/current-overlaps.json',),
        'semantics-core': ('checkpoint530-semantic.plan.json', 'checkpoint530-semantic.report.json', 'checkpoint530-semantic.predictions.json', 'models/checkpoint530-semantic.json'),
        'projection': ('soundtracks-all.unlabeled-projection.report.json', 'soundtracks-all.unlabeled-projection.oof.json', 'models/soundtracks-all.unlabeled-projection.json'),
        'multitask': ('multitask.report.json', 'multitask.oof.json', 'multitask.fold-audit.json'),
        'exports': ('export-replay.report.json',),
    }
    if step in ('consensus-core', 'consensus-selected', 'ranking-core'):
        kind = 'consensus' if step.startswith('consensus-') else 'vibe-ranking'
        slug = 'checkpoint530__' + '+'.join(CORE)
        if step == 'consensus-selected':
            slug += '__native-selected_learner__calibrator-selected_learner'
        names[step] = (f'{kind}-{slug}.report.json', f'{kind}-{slug}.oof.json', f'models/{kind}-{slug}.json')
    local_dataset = {'local-soundtracks': 'soundtracks-all', 'local-pmemo': 'pmemo'}.get(step)
    if local_dataset:
        names[step] = tuple(f'checkpoint-1/{local_dataset}.local-ablation.{kind}.json' for kind in ('report', 'oof'))
    required = list(names[step])
    if local_dataset:
        required += [f'checkpoint-1/models/local-ablation/{local_dataset}.{method}.fold{fold}.json'
                     for method in ('linear_no_local', 'local_neighbourhood_selected',
                         'external_soft_target_selected', 'shared_no_local', 'shared_local_normalization',
                         'stored_teacher_selected') for fold in range(5)]
    native_dataset = {'emotify-identities': 'emotify-artist', 'emotify-plus': 'emotify-plus-strict',
                      'merp': 'merp-static', 'deam': 'deam', 'emma': 'emma-preview-matched',
                      'emotify-windows': 'emotify-artist-multiwindow'}.get(step)
    native_datasets = (native_dataset,) if native_dataset else {
        'native-ready': READY, 'secondary': ('amg1608-matched', 'ntwicm-matched'),
        'merge': ('merge-quadrants', 'merge-tag-va'),
        'temporal-pmemo': ('pmemo-dynamic',), 'temporal-deam': ('deam-dynamic',)}.get(step, ())
    for dataset in native_datasets:
        required += [dataset + suffix for suffix in ('.splits.json', '.oof.csv', '.report.md')]
        required += [f'models/{dataset}.{family}.json' for family in (
            'attributes', 'embedding', 'pretrained_moods', 'attributes_embedding', 'attributes_embedding_moods')]
    learner_datasets = {'learners-ready': READY, 'learners-deam': ('deam',),
        'temporal-pmemo': ('pmemo-dynamic',), 'temporal-deam': ('deam-dynamic',)}.get(step, ())
    for dataset in learner_datasets:
        required += [f'models/learners/{dataset}.{family}.{kind}.json'
                     for family in ('attributes', 'embedding', 'attributes_embedding')
                     for kind in ('linear', 'rbf', 'neighbours', 'selected_learner')]
    if step in ('cross-corpus', 'multitask'):
        directory = OUT / 'models' / ('cross-corpus' if step == 'cross-corpus' else 'multitask')
        required += [str(path.relative_to(OUT)) for path in directory.glob('*.json')]
    if step == 'snapshot':
        manifest = json.loads((RUN / 'inputs/manifest.json').read_text())
        required += ['checkpoint-1/inputs/' + filename for filename in manifest['files']]
    if step == 'workflow':
        details = json.loads((RUN / 'listening-workflow-index.json').read_text())
        required += ['checkpoint-1/' + name for name in details.get('outputs', {})]
    if step == 'overlaps':
        details = json.loads((RUN / 'current-overlaps.json').read_text())
        required.append(str(Path(details['manifest']).relative_to(OUT)))
    return {name: digest(OUT / name) for name in required}


def reusable(entry, signature):
    if entry['state'] != 'complete' or entry['fingerprint'] != signature or not entry.get('outputs'):
        return False
    return all((OUT / name).is_file() and digest(OUT / name) == expected
               for name, expected in entry['outputs'].items())


def require_complete_corpus(dataset):
    """A partial cache is resumable input, never a completed source benchmark."""
    feature_path = OUT / (dataset + '.features.json')
    if not feature_path.exists():
        raise ValueError('Required corpus has no completed features: ' + dataset)
    source = json.loads(feature_path.read_text())
    if not source.get('rows'):
        raise ValueError('Required corpus has no usable rows: ' + dataset)
    index_path = OUT / (dataset + '.index.json')
    if index_path.exists():
        expected = {row['id'] for row in json.loads(index_path.read_text())['rows']}
        observed = {row['id'] for row in source['rows']}
        failures = {row['id'] for row in source.get('errors', [])}
        missing = expected - observed - failures
        if missing or (observed | failures) - expected or len(observed) != len(source['rows']):
            raise ValueError(f'Required corpus is partial: {dataset}; '
                             f'{len(observed)}/{len(expected)} usable, {len(missing)} unprocessed')


def input_corpora(step):
    if step in ('native-ready', 'learners-ready'):
        return READY
    if step in ('temporal-deam', 'learners-deam'):
        return ('deam',)
    if step == 'temporal-pmemo':
        return ('pmemo',)
    if step == 'cross-corpus':
        return ('pmemo', 'deam', 'amg1608-matched', 'ntwicm-matched')
    if step in ('consensus-core', 'consensus-selected', 'ranking-core', 'projection', 'multitask'):
        return CORE
    if step == 'semantics-core':
        return (*CORE, 'emma-preview-matched')
    if step == 'merge-expansion':
        return ('merge-quadrants', '4q-emotion')
    return ()


def require_inputs(step):
    for dataset in input_corpora(step):
        require_complete_corpus(dataset)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('/home/rfirmin/.local/share/yt-playlist/state.db'))
    parser.add_argument('--steps', nargs='+', choices=STEPS, default=list(STEPS))
    parser.add_argument('--keep-going', action='store_true', help='Record failed research stages and continue independent work')
    args = parser.parse_args(argv)
    RUN.mkdir(parents=True, exist_ok=True)
    status_path = RUN / 'status.json'
    state = json.loads(status_path.read_text()) if status_path.exists() else {'steps': []}
    lifetime = None
    with threadpool_limits(limits=1):
        state.update(state='running', pid=os.getpid(), started_at=stamp())
        completed = set()
        for step in args.steps:
            signature = fingerprint(step)
            complete = next((row for row in reversed(state['steps']) if row['name'] == step
                             and reusable(row, signature)), None)
            try:
                require_inputs(step)
            except ValueError:
                # Preserve old bounded-pilot reports, but do not reuse them as
                # evidence for a campaign expecting the complete source index.
                complete = None
            if complete:
                print(json.dumps({'checkpoint_step': step, 'state': 'reused', 'fingerprint': signature}), flush=True)
                completed.add(step)
                continue
            code_hashes = {str(path): digest(path) for path in Path(__file__).parent.glob('external_*.py')}
            entry = {'name': step, 'state': 'running', 'started_at': stamp(), 'fingerprint': signature,
                     'code_sha256': code_hashes}
            state['steps'].append(entry)
            state.update(active=step, updated_at=stamp())
            save(status_path, state)
            print(json.dumps({'checkpoint_step': step, 'state': 'running'}), flush=True)
            try:
                require_inputs(step)
                if step == 'emotify-identities':
                    from .external_emotify_identity import prepare
                    from .external_training import evaluate
                    prepare(lifetime)
                    evaluate('emotify-artist')
                elif step == 'snapshot':
                    freeze(args.db)
                elif step == 'rater-weights':
                    from .external_rater_weights import evaluate
                    evaluate('emotify-artist')
                elif step == 'cal500-ranking':
                    from .external_ranking import evaluate
                    evaluate('cal500-raw-activity')
                elif step == 'local-pool':
                    from .external_local_pool import prepare
                    prepare()
                elif step == 'propagation':
                    from .external_propagation import evaluate
                    evaluate()
                elif step == 'native-ready':
                    from .external_training import evaluate
                    for dataset in READY:
                        evaluate(dataset)
                elif step in ('learners-ready', 'learners-deam'):
                    from .external_learners import evaluate
                    for dataset in READY if step == 'learners-ready' else ('deam',):
                        evaluate(dataset)
                elif step == 'secondary':
                    from .external_secondary import prepare
                    from .external_training import evaluate
                    for dataset in ('amg1608-matched', 'ntwicm-matched'):
                        prepare(dataset)
                        evaluate(dataset)
                elif step == 'workflow':
                    from .external_workflow import evaluate
                    evaluate(args.db)
                elif step == 'reference':
                    from .reference_pipeline import evaluate
                    evaluate(lifetime)
                elif step in ('temporal-pmemo', 'temporal-deam'):
                    from .external_temporal import evaluate
                    evaluate(step.removeprefix('temporal-'))
                elif step == 'cross-corpus':
                    from .external_cross_corpus import evaluate
                    evaluate()
                elif step == 'merge':
                    from .external_experiment import extract
                    from .external_merge import rebind_tag_va
                    from .external_training import evaluate
                    extract('merge-quadrants', native=True, lifetime_fd=lifetime)
                    evaluate('merge-quadrants')
                    rebind_tag_va()
                    evaluate('merge-tag-va')
                elif step == 'merge-expansion':
                    from .external_merge import compare_4q
                    compare_4q()
                elif step == 'overlaps':
                    from .external_overlap_manifest import freeze as freeze_overlaps
                    freeze_overlaps()
                elif step in ('consensus-core', 'consensus-selected'):
                    from .external_consensus import evaluate
                    kind = 'selected_learner' if step == 'consensus-selected' else None
                    evaluate(list(CORE), native_learner=kind or 'ridge', calibrator=kind or 'linear',
                             identity_exclusions=protected_identities(), run_name='checkpoint530')
                elif step == 'ranking-core':
                    from .external_vibe_ranking import evaluate
                    evaluate(list(CORE), identity_exclusions=protected_identities(), run_name='checkpoint530')
                elif step == 'semantics-core':
                    from .external_semantic import evaluate
                    evaluate(['emotify-artist', 'pmemo', 'deam', 'cal500-raw-activity', 'emma-preview-matched'],
                             identity_exclusions=protected_identities(), run_name='checkpoint530-semantic')
                elif step == 'projection':
                    from .external_unlabeled_projection import evaluate
                    evaluate('soundtracks-all', list(CORE), identity_exclusions=protected_identities())
                elif step == 'multitask':
                    from .external_multitask import evaluate
                    evaluate(list(CORE), identity_exclusions=protected_identities())
                elif step == 'exports':
                    from .external_export_check import evaluate
                    slug = 'checkpoint530__' + '+'.join(CORE)
                    evaluate([OUT / 'models' / name for name in (
                        'consensus-' + slug + '.json',
                        'consensus-' + slug + '__native-selected_learner__calibrator-selected_learner.json',
                        'vibe-ranking-' + slug + '.json')])
                elif step in ('local-soundtracks', 'local-pmemo'):
                    from .external_local_ablation import evaluate
                    evaluate('soundtracks-all' if step == 'local-soundtracks' else 'pmemo')
                elif step == 'emotify-plus':
                    from .external_emotify_plus import prepare
                    from .external_training import evaluate
                    prepare()
                    evaluate('emotify-plus-strict')
                elif step == 'emotify-windows':
                    from .external_experiment import extract
                    from .external_emotify_identity import row_metadata
                    from .external_multiwindow import aggregate
                    from .external_training import evaluate
                    for position, fraction in (('first', .05), ('last', .95)):
                        extract('emotify', native=True, sample_fraction=fraction,
                                output_dataset='emotify-artist-' + position, lifetime_fd=lifetime,
                                row_metadata=row_metadata())
                    evaluate(aggregate('emotify-artist'))
                elif step in ('deam', 'emma', 'merp'):
                    from .external_experiment import extract
                    from .external_training import evaluate
                    dataset = {'deam': 'deam', 'emma': 'emma-preview-matched', 'merp': 'merp-static'}[step]
                    extract(dataset, native=True, lifetime_fd=lifetime)
                    evaluate(dataset)
                if any(not Path(path).exists() or digest(path) != expected
                       for path, expected in code_hashes.items()):
                    raise RuntimeError('Research code changed during this stage; rerun with a stable implementation')
                entry.update(state='complete', finished_at=stamp(), fingerprint=fingerprint(step),
                             outputs=outputs(step))
                completed.add(step)
            except BaseException as error:
                entry.update(state='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                             error=repr(error), traceback=traceback.format_exc(), finished_at=stamp())
                state.update(state=entry['state'], updated_at=stamp())
                save(status_path, state)
                if args.keep_going and isinstance(error, Exception):
                    print(json.dumps({'checkpoint_step': step, 'state': 'failed', 'error': repr(error)}), flush=True)
                    continue
                raise
            state.update(updated_at=stamp())
            save(status_path, state)
        state.update(state='complete' if set(args.steps) <= completed else 'partial',
                     active=None, updated_at=stamp())
        save(status_path, state)
        if state['state'] != 'complete':
            raise SystemExit('Checkpoint contains failed stages; inspect ' + str(status_path))


if __name__ == '__main__':
    main()
