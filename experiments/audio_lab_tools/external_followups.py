"""Sequential evidence-driven follow-ups after the expanded extraction campaign."""
import fcntl
import json
import os
import time
import traceback

from .external_experiment import OUT, digest, extract, save, stamp

STATUS = OUT / 'followups-status.json'
SOURCES = ['soundtracks-all', 'cal500', 'emotify', 'pmemo', 'vgmidi-original',
           'jamendo-human-4shards', 'coimbra-mirex-like', '4q-emotion', 'deam']


def main():
    state = json.loads(STATUS.read_text()) if STATUS.exists() else {'steps': [], 'started_at': stamp()}
    state.update(state='waiting_for_expanded_campaign', pid=os.getpid(), updated_at=stamp())
    save(STATUS, state)
    while True:
        path = OUT / 'campaign-status.json'
        campaign = json.loads(path.read_text()) if path.exists() else {}
        if campaign.get('state', '').startswith('complete'):
            break
        if campaign.get('state') == 'interrupted':
            raise RuntimeError('Expanded campaign was interrupted; do not silently train on incomplete features')
        time.sleep(15)

    # Imports happen after waiting: follow-up implementations can be reviewed
    # while the extractor runs without retaining stale loaded module versions.
    from .external_classification import evaluate as classification
    from .external_consistency import compare_deduplicated
    from .external_consensus import evaluate as consensus
    from .external_results import write_summary
    from .external_secondary import prepare as secondary
    from .external_training import evaluate as native
    from .external_learners import evaluate as learners
    from .external_multitask import evaluate as multitask
    from .external_multiwindow import evaluate as multiwindow
    from .external_merge import stage as merge_stage, rebind_tag_va, compare_4q
    from .external_cross_corpus import evaluate as cross_corpus
    from .external_rater_weights import evaluate as rater_weights
    from .external_semantic import evaluate as semantic
    from .external_export_check import evaluate as export_check
    from .external_vibe_ranking import evaluate as vibe_ranking
    from .external_diagnostics import write as diagnostics
    from .external_temporal import evaluate as temporal
    from .external_unlabeled_projection import evaluate as projection
    from .external_transfer import evaluate_transfer

    def run(name, operation):
        if any(row['name'] == name and row['state'] == 'complete' for row in state['steps']):
            return
        row = {'name': name, 'state': 'running', 'started_at': stamp()}
        state['steps'].append(row)
        state.update(state='running', active=name, updated_at=stamp(), script_sha256=digest(__file__))
        save(STATUS, state)
        print(json.dumps({'followup': name, 'state': 'running', 'at': stamp()}), flush=True)
        try:
            operation()
            row['state'] = 'complete'
        except KeyboardInterrupt:
            row.update(state='interrupted', finished_at=stamp())
            state.update(state='interrupted', updated_at=stamp())
            save(STATUS, state)
            raise
        except Exception as error:
            row.update(state='failed', error=repr(error), traceback=traceback.format_exc())
            print(traceback.format_exc(), flush=True)
        row['finished_at'] = stamp()
        state['updated_at'] = stamp()
        save(STATUS, state)
        write_summary()

    run('unlabeled:deduplicated-consistency', compare_deduplicated)
    # The publisher's current EMMA previews are a separate, explicitly weak
    # audio/longer-excerpt-label experiment. Downloading runs while extraction
    # above owns the single heavy worker; this stage waits its turn.
    def emma_native():
        extract('emma-preview-matched', native=True)
        native('emma-preview-matched')
        learners('emma-preview-matched')
    run('native:emma-preview-matched', emma_native)
    if any(row['name'] == 'native:emma-preview-matched' and row['state'] == 'complete' for row in state['steps']):
        SOURCES.append('emma-preview-matched')
        run('transfer:emma-preview-matched', lambda: evaluate_transfer(SOURCES))
        run('consensus:emotify-pmemo-emma', lambda: consensus(['emotify', 'pmemo', 'emma-preview-matched']))
        run('vibe-ranking:emotify-pmemo-emma', lambda: vibe_ranking(['emotify', 'pmemo', 'emma-preview-matched']))
    for dataset in ('amg1608-matched', 'ntwicm-matched'):
        def matched_native(dataset=dataset):
            secondary(dataset)
            native(dataset)
            learners(dataset)
        run('secondary-native:' + dataset, matched_native)
        if (OUT / f'{dataset}.report.json').exists():
            run('secondary-transfer:' + dataset, lambda dataset=dataset: evaluate_transfer([*SOURCES, dataset]))
    matched_sources = [name for name in ('amg1608-matched', 'ntwicm-matched') if (OUT / f'{name}.report.json').exists()]
    if matched_sources:
        run('secondary-consensus:all', lambda: consensus([*SOURCES, *matched_sources]))
    run('vibe-ranking:expanded', lambda: vibe_ranking(SOURCES))
    if matched_sources:
        run('vibe-ranking:secondary', lambda: vibe_ranking([*SOURCES, *matched_sources]))
    for dataset in ('coimbra-mirex-like', '4q-emotion'):
        run('classification:' + dataset, lambda dataset=dataset: classification(dataset))
    for dataset in ('pmemo', 'deam'):
        run('temporal:' + dataset, lambda dataset=dataset: temporal(dataset))
        alias = dataset + '-dynamic'
        if (OUT / f'{alias}.report.json').exists():
            sources = [alias if name == dataset else name for name in SOURCES]
            run('temporal-transfer:' + dataset, lambda sources=sources: evaluate_transfer(sources))
            run('temporal-consensus:' + dataset, lambda sources=sources: consensus(sources))
    if all((OUT / f'{name}-dynamic.report.json').exists() for name in ('pmemo', 'deam')):
        sources = [name + '-dynamic' if name in ('pmemo', 'deam') else name for name in SOURCES]
        run('temporal-consensus:both', lambda: consensus(sources))
    for dataset in ('gold-vibes', 'soundtracks-all', 'emotify', 'cal500', 'pmemo', 'deam', '4q-emotion', 'coimbra-mirex-like'):
        run('unlabeled-projection:' + dataset, lambda dataset=dataset: projection(dataset, SOURCES))
    run('shared-native-representation', lambda: multitask([*SOURCES, *matched_sources]))
    run('native-listener-count-weighting:emotify', rater_weights)
    run('cross-corpus:native-valence-arousal', cross_corpus)
    run('fixed-semantic-mapping:no-gold-fitting', semantic)
    run('multiwindow:emotify', multiwindow)
    run('acquire:merge-official-archive', merge_stage)
    if any(row['name'] == 'acquire:merge-official-archive' and row['state'] == 'complete' for row in state['steps']):
        def merge_native():
            extract('merge-quadrants', native=True)
            native('merge-quadrants')
            rebind_tag_va()
            native('merge-tag-va')
        run('native:merge-separate-targets', merge_native)
        run('classification:merge-quadrants', lambda: classification('merge-quadrants', ('embedding',)))
        run('native-expansion:4q-plus-merge', compare_4q)
        if (OUT / 'merge-quadrants.report.json').exists():
            for replacement in ('merge-quadrants', 'merge-tag-va'):
                sources = [replacement if name == '4q-emotion' else name for name in SOURCES]
                run('transfer:' + replacement, lambda sources=sources: evaluate_transfer(sources))
                run('consensus:' + replacement, lambda sources=sources: consensus(sources))
                run('vibe-ranking:' + replacement, lambda sources=sources: vibe_ranking(sources))
    run('diagnostics:gold-coverage', diagnostics)
    run('export:independent-replay', export_check)
    latest = {row['name']: row['state'] for row in state['steps']}
    state.update(state='complete_with_failures' if any(value != 'complete' for value in latest.values()) else 'complete',
                 active=None, updated_at=stamp())
    save(STATUS, state)
    write_summary()


if __name__ == '__main__':
    # The waiting process owns no expensive resources and takes no test lock.
    # The main campaign's separate lock prevents two extractor campaigns.
    with (OUT / 'followups.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()
