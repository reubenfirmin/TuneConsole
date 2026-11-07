"""Finish the remaining #530 comparisons with pinned inputs and one test owner.

The newly acquired ManyMusic/Memo cohorts remain external evaluation inputs;
they never enter this campaign's training or subset selection.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import traceback

from threadpoolctl import threadpool_limits

from .. import TOOLS, source_path
from .external_checkpoint import CORE, protected_identities, require_complete_corpus
from ..support.external_experiment import OUT, ROOT, digest, save, stamp
from ..evaluation.external_transfer import SNAPSHOT, excluded_identities
from ..support.research_artifacts import compact_slug

WORK = OUT / 'checkpoint-1/completion'
EXTRA = ('coimbra-mirex-like', 'merge-quadrants', 'emma-preview-matched',
         'merp-static', 'amg1608-matched', 'ntwicm-matched')
SOURCES = (*CORE, *EXTRA)
VARIANTS = {
    'both-aligned': tuple(name + '-dynamic' if name in ('pmemo', 'deam') else name for name in CORE),
    'perceived': tuple('emotify-plus-strict' if name == 'emotify-artist' else name for name in CORE),
    'windows': tuple('emotify-artist-multiwindow' if name == 'emotify-artist' else name for name in CORE),
}
LEARNERS = ('emma-preview-matched', 'amg1608-matched', 'ntwicm-matched', 'merp-static',
            'emotify-plus-strict', 'emotify-artist-multiwindow', 'coimbra-mirex-like')
PROJECTIONS = ('gold-vibes', 'soundtracks-all', 'emotify-artist', 'cal500-raw-activity',
               'pmemo', 'deam', 'merge-quadrants', 'coimbra-mirex-like')
STEPS = ('consistency', *(f'learners:{name}' for name in LEARNERS),
         'classification:coimbra-mirex-like', 'classification:4q-emotion', 'classification:merge-quadrants',
         *(f'projection:{name}' for name in PROJECTIONS),
         *(f'consensus:{name}' for name in VARIANTS), 'consensus:expanded', 'ranking:expanded',
         'semantics:expanded', 'multitask:expanded')


def inspect():
    pairs = json.loads((OUT / 'unlabeled-consistency/pairs.json').read_text())['pairs']
    index = json.loads((OUT / 'pmemo.features.json').read_text())['rows']
    pair_ids = {pair['id'] for pair in pairs}
    overlaps = excluded_identities([row for row in index if row['id'] in pair_ids], protected_identities())
    state = json.loads((OUT / 'checkpoint-1/status.json').read_text())
    latest = {row['name']: row for row in state['steps']}
    result = {'created_at': stamp(), 'sources': SOURCES, 'expert_subsets': 2 ** (len(SOURCES) + 1) - 1,
        'steps': STEPS, 'unlabeled_pair_protected_identity_overlaps': [
            {key: row.get(key) for key in ('id', 'artist', 'title')} for row in overlaps],
        'finished_main_stages': {name: {key: row.get(key) for key in ('state', 'started_at', 'finished_at')}
                                 for name, row in latest.items()}}
    save(WORK / 'inventory.json', result)
    print(json.dumps(result), flush=True)


def stage_outputs(step):
    kind, _, name = step.partition(':')
    if step == 'consistency':
        return list((OUT / 'unlabeled-consistency-deduplicated').rglob('*.json'))
    if kind in ('learners', 'classification'):
        return [OUT / f'{name}.{kind}.{suffix}.json' for suffix in ('report', 'oof')] + list(
            (OUT / f'models/{kind}').glob(name + '.*.json'))
    if kind == 'projection':
        result = [OUT / f'{name}.unlabeled-projection.{suffix}.json' for suffix in ('report', 'oof')]
        result.append(OUT / f'models/{name}.unlabeled-projection.json')
        if name == 'gold-vibes':
            result.extend(OUT / f'gold-vibes.unlabeled-projection-ranking.{suffix}.json' for suffix in ('report', 'oof'))
            result.append(OUT / 'models/gold-vibes.unlabeled-projection-ranking.json')
        return result
    if kind in ('consensus', 'ranking'):
        sources = SOURCES if name == 'expanded' else VARIANTS[name]
        slug = compact_slug('checkpoint530-completion-' + name + '__' + '+'.join(sources))
        prefix = 'vibe-ranking' if kind == 'ranking' else 'consensus'
        return [OUT / f'{prefix}-{slug}.{suffix}.json' for suffix in ('report', 'oof')] + [
            OUT / f'models/{prefix}-{slug}.json']
    if kind == 'semantics':
        return [OUT / f'checkpoint530-completion-semantic.{suffix}.json' for suffix in ('plan', 'report', 'predictions')] + [
            OUT / 'models/checkpoint530-completion-semantic.json']
    if kind == 'multitask':
        return [OUT / f'multitask.{suffix}.json' for suffix in ('report', 'oof', 'fold-audit')] + list(
            (OUT / 'models/multitask').glob('*.json')) + list(OUT.glob('multitask-*.json')) + list(
            (OUT / 'models').glob('multitask-*.json'))
    raise ValueError(step)


def inputs(step):
    # Bind code, weight provenance, all usable corpus records and every actual
    # feature file. Target judgments in ManyMusic/Memo are deliberately absent.
    paths = [Path(__file__), source_path('research_artifacts.py'), *TOOLS.rglob('external_*.py'),
             *TOOLS.rglob('__init__.py'),
             SNAPSHOT, OUT / 'checkpoint-1/inputs/manifest.json',
             OUT / 'checkpoint-1/inputs/human-labels.json',
             OUT / 'unlabeled-consistency/pairs.json', OUT / 'unlabeled-consistency/pair-split.json']
    corpora = set(SOURCES) | {name for sources in VARIANTS.values() for name in sources} | {'4q-emotion'}
    feature_paths = set()
    for name in sorted(corpora):
        require_complete_corpus(name)
        path = OUT / f'{name}.features.json'
        paths.extend((path, OUT / f'models/{name}.embedding.json'))
        feature_paths.update(Path(row['feature_file']) for row in json.loads(path.read_text())['rows'])
    paths.extend(sorted(feature_paths))
    hashes = {str(path.relative_to(ROOT)): digest(path) for path in paths}
    return {'step': step, 'sha256': hashes, 'source_experts': SOURCES, 'variants': VARIANTS,
            'new_audio_targets_used_for_training': False}


def operation(step):
    kind, _, name = step.partition(':')
    protected = protected_identities()
    if step == 'consistency':
        from ..training.external_consistency import compare_deduplicated
        return compare_deduplicated(identity_exclusions=protected)
    if kind == 'learners':
        from ..training.external_learners import evaluate
        return evaluate(name)
    if kind == 'classification':
        from ..training.external_classification import evaluate
        return evaluate(name, ('embedding',) if name == 'merge-quadrants' else None)
    if kind == 'projection':
        from ..training.external_unlabeled_projection import evaluate
        return evaluate(name, SOURCES, identity_exclusions=protected)
    if kind == 'consensus':
        from ..training.external_consensus import evaluate
        sources = SOURCES if name == 'expanded' else VARIANTS[name]
        return evaluate(list(sources), identity_exclusions=protected, run_name='checkpoint530-completion-' + name)
    if kind == 'ranking':
        from ..evaluation.external_vibe_ranking import evaluate
        return evaluate(list(SOURCES), identity_exclusions=protected, run_name='checkpoint530-completion-' + name)
    if kind == 'semantics':
        from ..training.external_semantic import evaluate
        return evaluate(['emotify-artist-multiwindow', 'emma-preview-matched',
                         'pmemo', 'deam', 'merp-static', 'amg1608-matched', 'ntwicm-matched',
                         'cal500-raw-activity'], identity_exclusions=protected,
                        run_name='checkpoint530-completion-semantic')
    if kind == 'multitask':
        from ..training.external_multitask import evaluate
        return evaluate(list(SOURCES), identity_exclusions=protected)
    raise ValueError(step)


def run(steps):
    status_path = WORK / 'status.json'
    state = json.loads(status_path.read_text()) if status_path.exists() else {'steps': []}
    state.update(state='running', pid=os.getpid(), started_at=stamp())
    with threadpool_limits(limits=1):
        for step in steps:
            provenance = inputs(step)
            fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
            previous = next((row for row in reversed(state['steps']) if row['name'] == step
                and row['state'] == 'complete' and row['fingerprint'] == fingerprint
                and row.get('outputs') and all((ROOT / path).is_file() and digest(ROOT / path) == expected
                    for path, expected in row['outputs'].items())), None)
            if previous:
                print(json.dumps({'completion_stage': step, 'state': 'reused'}), flush=True)
                continue
            entry = {'name': step, 'state': 'running', 'started_at': stamp(),
                     'fingerprint': fingerprint, 'inputs': provenance}
            state['steps'].append(entry)
            state.update(active=step, updated_at=stamp())
            save(status_path, state)
            archive = WORK / 'prior-artifacts' / step.replace(':', '-') / fingerprint
            if not archive.exists():
                archive.mkdir(parents=True)
                for path in stage_outputs(step):
                    if path.is_file():
                        destination = archive / path.relative_to(OUT)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
                save(archive / 'manifest.json', {'files': {str(path.relative_to(archive)): digest(path)
                    for path in archive.rglob('*') if path.is_file()}})
            print(json.dumps({'completion_stage': step, 'state': 'running'}), flush=True)
            try:
                operation(step)
                current = inputs(step)
                if current != provenance:
                    raise ValueError('Stage inputs changed while running: ' + step)
                outputs = stage_outputs(step)
                if not outputs or any(not path.is_file() for path in outputs):
                    raise ValueError('Stage did not produce every required output: ' + step)
                entry.update(state='complete', outputs={str(path.relative_to(ROOT)): digest(path) for path in outputs})
            except BaseException as error:
                entry.update(state='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                             error=repr(error), traceback=traceback.format_exc(), finished_at=stamp())
                state.update(state=entry['state'], active=None, updated_at=stamp())
                save(status_path, state)
                raise
            entry['finished_at'] = stamp()
            state.update(updated_at=stamp())
            save(status_path, state)
            print(json.dumps({'completion_stage': step, 'state': 'complete'}), flush=True)
    latest = {row['name']: row['state'] for row in state['steps']}
    state.update(state='complete' if all(latest.get(step) == 'complete' for step in STEPS) else 'partial',
                 active=None, updated_at=stamp())
    save(status_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', action='store_true', help='Inventory pending comparisons and identity conflicts without training')
    parser.add_argument('--steps', nargs='+', choices=STEPS, default=list(STEPS))
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if args.inspect:
        inspect()
        return
    with (WORK / '.campaign.lock').open('a+') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another completion coordinator owns this campaign')
        run(args.steps)


if __name__ == '__main__':
    main()
